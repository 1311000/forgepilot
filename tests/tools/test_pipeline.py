"""The seven-phase pipeline: ordering, refusals, and what survives a failure.

The assertions worth having here are the ones about things *not* happening — a
guard denial that never reaches the sandbox, a refusal that is still logged, a
raising hook that does not kill the call — because those are the paths that
degrade silently when someone reorders a phase.
"""

from __future__ import annotations

import json


from forgepilot.runtime.guards import GuardDecision, GuardResult
from forgepilot.runtime.tools.pipeline import Phase
from forgepilot.seams.approval import ApprovalOutcome
from forgepilot.seams.refs import SessionId, TurnId
from forgepilot.seams.security import Operation
from forgepilot.seams.session import EventKind
from forgepilot.seams.tools import (
    ResourceTargets,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolOutcome,
)

def constraints_for(
    workspace,
    *,
    operations=(
        Operation.READ_CODE,
        Operation.WRITE_CODE,
        Operation.RUN_TESTS,
        Operation.OPEN_PR,
    ),
    writable=("src",),
):
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import ExecutionConstraints, Principal

    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        permitted_operations=frozenset(operations),
        writable_paths=writable,
    )

TURN = TurnId(1)

#: The id the ``rig`` fixture opens its session under.
SESSION_ID = SessionId("s-under-test")


def call(name: str, arguments: dict | str = "{}") -> ToolCall:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ToolCall(call_id=f"c-{name}", name=name, arguments_json=raw)


def outcomes(rig):
    return [e for e in rig.session.events if e.kind == EventKind.TOOL_RESULT]


def calls(rig):
    return [e for e in rig.session.events if e.kind == EventKind.TOOL_CALL]


async def invoke(rig, c, **kwargs):
    return await rig.pipeline.invoke(
        c,
        session=rig.session,
        turn=TURN,
        step=1,
        constraints=kwargs.pop("constraints", constraints_for(rig.workspace)),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
async def test_phases_run_in_the_documented_order(rig) -> None:
    (rig.workspace / "src" / "a.txt").write_text("hello\n", encoding="utf-8")

    await invoke(rig, call("read_file", {"path": "src/a.txt"}))

    assert rig.pipeline.trace == [
        Phase.TOOL_CALL,
        Phase.PRE_EXECUTE,
        Phase.GUARDS,
        Phase.ARM,
        Phase.EXECUTE,
        Phase.POST_EXECUTE,
        Phase.FINALIZE,
        Phase.TOOL_RESULT,
    ]


async def test_the_call_is_recorded_before_it_runs(rig) -> None:
    """A crash mid-call must leave a log that says the call happened.

    Otherwise recovery sees a result with no call, or nothing at all, and cannot
    tell "never started" from "started and died".
    """
    seen: list[bool] = []

    async def spy(call_, ctx):
        seen.append(any(e.kind == EventKind.TOOL_CALL for e in rig.session.committed_events))
        return ToolOutcome(content="ok")

    rig.registry.register(
        ToolDefinition(
            name="spy",
            description="",
            parameters={},
            effects=frozenset({ToolEffect.READ}),
            execute=spy,
        )
    )

    await invoke(rig, call("spy"))

    assert seen == [True], "the tool body ran before its call was durable"


async def test_call_and_result_share_a_call_id(rig) -> None:
    (rig.workspace / "src" / "a.txt").write_text("x", encoding="utf-8")
    await invoke(rig, call("read_file", {"path": "src/a.txt"}))

    assert calls(rig)[0].data["callId"] == outcomes(rig)[-1].data["callId"]


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
async def test_a_guard_denial_never_reaches_the_sandbox(rig) -> None:
    rig.approval.set_answerer(lambda r: _allow())

    outcome = await invoke(rig, call("run", {"command": "echo hi"}), constraints=constraints_for(
        rig.workspace, operations=(Operation.READ_CODE,)  # no RUN_TESTS
    ))

    assert outcome.is_error
    assert outcome.meta["phase"] == "guards"
    assert "execute" not in rig.pipeline.trace
    assert rig.sandbox.calls == []


async def test_a_guard_registered_mid_call_does_not_apply_to_it(rig) -> None:
    """The snapshot is taken at ``tool/call`` time.

    Otherwise a plugin could register a guard (or remove one) while a call was in
    flight and change the verdict after the fact.
    """
    seen: list[int] = []

    async def sneak(call_, ctx):
        # Register a denying guard *during* the call, then check whether it bit.
        rig.guards.register("late", lambda view: GuardResult(GuardDecision.DENY, "too late"))
        seen.append(len(rig.guards.snapshot().guards))
        return ToolOutcome(content="ok")

    rig.registry.register(
        ToolDefinition(
            name="sneak", description="", parameters={},
            effects=frozenset({ToolEffect.READ}), execute=sneak,
        )
    )

    outcome = await invoke(rig, call("sneak"))

    assert outcome.is_error is False, "the late guard must not have refused this call"
    assert seen and seen[0] > 0


async def test_a_repo_outside_the_allow_list_is_refused(rig) -> None:
    from forgepilot.seams.refs import RepoRef

    secret = RepoRef(host="git.corp", namespace="other", name="secret")
    rig.registry.register(
        ToolDefinition(
            name="peek", description="", parameters={},
            effects=frozenset({ToolEffect.READ}),
            execute=lambda c, ctx: _ok("peeked"),
            describe_targets=lambda args: ResourceTargets(repos=(secret,)),
        )
    )

    outcome = await invoke(rig, call("peek"))

    assert outcome.meta["phase"] == "guards"
    assert outcome.meta["guard"] == "repo-scope"
    assert "execute" not in rig.pipeline.trace


async def test_a_write_outside_the_writable_set_is_refused(rig) -> None:
    outcome = await invoke(rig, call("write_file", {"path": "docs/x.md", "content": "hi"}))

    assert outcome.is_error
    assert outcome.meta["phase"] == "guards"
    assert not (rig.workspace / "docs").exists(), "nothing may have been written"


async def test_a_path_escaping_the_workspace_is_refused(rig) -> None:
    outcome = await invoke(rig, call("read_file", {"path": "../../etc/passwd"}))

    assert outcome.is_error
    assert outcome.meta["phase"] == "guards"


async def test_the_budget_guard_refuses_once_exhausted(rig) -> None:
    """A budget is a guard, not a hint: exceeding it is final."""
    rig.tracker.tool_calls = 10_000  # over the default max_tool_calls

    outcome = await invoke(rig, call("read_file", {"path": "src/a.txt"}))

    assert outcome.meta.get("guard") == "budget"
    assert "execute" not in rig.pipeline.trace


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------
async def test_approval_allowed_once_lets_the_call_proceed(rig) -> None:
    """An allow reaches the sandbox — which is the assertion; whether the command
    then succeeds is the sandbox's business, and the recording one honestly
    reports that it executed nothing."""
    rig.approval.set_answerer(lambda r: _allow())
    rig.pipeline.on_pre_execute(lambda p, s: _ask("dangerous"))

    outcome = await invoke(rig, call("run", {"command": "echo hi"}))

    assert Phase.APPROVAL in rig.pipeline.trace
    assert "execute" in rig.pipeline.trace
    # The model's line reaches the sandbox **as a shell line** — `run` wraps it so
    # a model can write `a && b`, which is what one actually wrote. That the
    # wrapping is the platform's own shell is `test_run_command`'s business; here
    # the assertion is that the text arrived unaltered inside it.
    assert rig.sandbox.calls
    assert rig.sandbox.calls[0].argv[-1] == "echo hi"

    # And the outcome says plainly that nothing ran, rather than reporting a
    # successful `(exit 0)` for a command that never happened.
    assert outcome.meta.get("executed") is False
    assert "NOT executed" in outcome.content


async def test_silence_is_a_refusal(rig) -> None:
    """No answerer at all — fail-closed."""
    rig.pipeline.on_pre_execute(lambda p, s: _ask("dangerous"))

    outcome = await invoke(rig, call("run", {"command": "echo hi"}))

    assert outcome.meta["outcome"] == "unavailable"
    assert rig.sandbox.calls == []


async def test_an_answerer_that_raises_is_not_a_yes(rig) -> None:
    async def broken(request):
        raise RuntimeError("the UI exploded")

    rig.approval.set_answerer(broken)
    rig.pipeline.on_pre_execute(lambda p, s: _ask("dangerous"))

    outcome = await invoke(rig, call("run", {"command": "echo hi"}))

    assert outcome.meta["outcome"] == "unavailable"
    assert rig.sandbox.calls == []


async def test_never_policy_short_circuits_the_answerer(rig) -> None:
    """A policy of never cannot be routed around by registering an answerer."""
    from forgepilot.runtime.approval import ApprovalServicePlugin
    from forgepilot.seams.approval import ApprovalPolicy

    rig.approval._policy = ApprovalPolicy.NEVER
    rig.approval.set_answerer(lambda r: _allow())
    rig.pipeline.on_pre_execute(lambda p, s: _ask("dangerous"))

    outcome = await invoke(rig, call("run", {"command": "echo hi"}))

    assert outcome.meta["outcome"] == "unavailable"
    assert rig.approval.decided[-1][1] is ApprovalOutcome.UNAVAILABLE


async def test_approval_is_audited_but_log_only(rig) -> None:
    """The model is never told a call was proposed and declined."""
    from forgepilot.seams.session import SurfaceOp

    rig.approval.set_answerer(lambda r: _deny())
    rig.pipeline.on_pre_execute(lambda p, s: _ask("dangerous"))
    await invoke(rig, call("run", {"command": "echo hi"}))

    asked = [e for e in rig.session.events if e.kind == EventKind.APPROVAL_ASKED]
    decided = [e for e in rig.session.events if e.kind == EventKind.APPROVAL_DECIDED]
    assert len(asked) == 1 and len(decided) == 1
    assert asked[0].surface is SurfaceOp.LOG_ONLY
    assert decided[0].surface is SurfaceOp.LOG_ONLY


async def test_the_prompt_is_durable_while_the_human_is_being_asked(rig) -> None:
    """``approval/asked`` must be in storage *before* the answerer blocks.

    The prompt is how a human — or an HTTP client — learns there is something to
    answer. That happens on a *different* request, and it learns it by reading
    the log. If the event is still in the session's buffer while the answerer
    waits, the prompt is invisible, nobody answers, and the turn hangs forever
    with no error to diagnose.

    Read from the **store**, not from ``session.events``: the latter includes the
    buffer, so asserting against it would pass whether or not anything was
    durable. This is the same rule ``tool/call`` already follows — durable before
    the thing that can run away from you.
    """
    at_prompt: list[list[str]] = []

    async def answerer(request):
        at_prompt.append([e.kind async for e in rig.store.read(SESSION_ID)])
        return ApprovalOutcome.ALLOWED_ONCE

    rig.approval.set_answerer(answerer)
    rig.pipeline.on_pre_execute(lambda p, s: _ask("dangerous"))
    await invoke(rig, call("run", {"command": "echo hi"}))

    assert at_prompt, "the answerer was never consulted"
    assert EventKind.APPROVAL_ASKED in at_prompt[0], (
        "the prompt was not durable while the answerer was blocked, so nothing "
        "reading the log could have known there was something to answer"
    )
    # And the tool body is genuinely still ahead of us at that moment.
    assert EventKind.TOOL_RESULT not in at_prompt[0]


# ---------------------------------------------------------------------------
# Captured capabilities
# ---------------------------------------------------------------------------
async def test_a_tool_is_not_granted_what_it_did_not_declare(rig) -> None:
    """A read-only tool that tries to write finds no capability to do it with."""
    from forgepilot.kernel.errors import CapabilityNotGranted

    rig.registry.register(
        ToolDefinition(
            name="liar", description="", parameters={},
            effects=frozenset({ToolEffect.READ}),
            execute=lambda c, ctx: _require_write(ctx),
        )
    )

    outcome = await invoke(rig, call("liar"))

    assert outcome.is_error
    assert "CapabilityNotGranted" in outcome.content


async def test_network_is_withheld_when_constraints_deny_it(rig) -> None:
    from forgepilot.runtime.tools.arm import ArmScope, mint_capabilities
    from forgepilot.seams.sandbox import SandboxEnforcement

    granted = mint_capabilities(
        effects=frozenset({ToolEffect.NETWORK, ToolEffect.READ}),
        scope=ArmScope(
            session_id=rig.session.header.session_id,
            constraints=constraints_for(rig.workspace),  # network=DENY
            enforcement=SandboxEnforcement.FULL,
        ),
    )

    assert granted.network is False
    assert granted.fs_read is True


# ---------------------------------------------------------------------------
# Failures are contained
# ---------------------------------------------------------------------------
async def test_an_unknown_tool_is_an_error_result_not_a_crash(rig) -> None:
    outcome = await invoke(rig, call("no_such_tool"))

    assert outcome.is_error
    assert "no such tool" in outcome.content
    assert outcomes(rig)[-1].data["isError"] is True


async def test_malformed_arguments_are_reported_and_still_logged(rig) -> None:
    """The raw string is kept so the failure stays replayable."""
    outcome = await invoke(rig, call("read_file", "{not json"))

    assert outcome.is_error
    assert "could not parse arguments" in outcome.content
    assert calls(rig)[-1].data["arguments"] == "{not json"


async def test_a_raising_tool_becomes_an_error_result(rig) -> None:
    async def explode(c, ctx):
        raise ValueError("boom")

    rig.registry.register(
        ToolDefinition(
            name="boom", description="", parameters={},
            effects=frozenset({ToolEffect.READ}), execute=explode,
        )
    )

    outcome = await invoke(rig, call("boom"))

    assert outcome.is_error
    assert "ValueError: boom" in outcome.content
    assert outcomes(rig)[-1].data["isError"] is True


async def test_a_raising_hook_does_not_kill_the_call(rig) -> None:
    def bad_hook(preview, scope):
        raise RuntimeError("hook exploded")

    rig.pipeline.on_pre_execute(bad_hook)
    outcome = await invoke(rig, call("read_file", {"path": "src/a.txt"}))

    assert outcome.is_error
    assert outcome.meta["phase"] == "pre-execute"
    assert "hook exploded" in outcome.content


async def test_a_raising_post_execute_hook_does_not_lose_the_original_failure(rig) -> None:
    def bad_hook(call_, outcome):
        raise RuntimeError("post exploded")

    rig.pipeline.on_post_execute(bad_hook)
    outcome = await invoke(rig, call("read_file", {"path": "nope.txt"}))

    assert outcome.is_error
    assert outcomes(rig), "a result was still recorded"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _allow():
    return ApprovalOutcome.ALLOWED_ONCE


def _deny():
    return ApprovalOutcome.REJECTED


def _ask(reason):
    from forgepilot.runtime.tools.pre_execute import PreDecision, PreDecisionKind

    return PreDecision(PreDecisionKind.ASK, reason=reason)


async def _ok(text):
    return ToolOutcome(content=text)


async def _require_write(ctx):
    from forgepilot.kernel.errors import CapabilityNotGranted

    if not ctx.capabilities.fs_write:
        raise CapabilityNotGranted("no write capability was granted")
    return ToolOutcome(content="wrote")


# ---------------------------------------------------------------------------
# Honest failures
#
# Both of these were found by running a real model against the skeleton, which
# is the only thing that reliably finds "the tool lied" bugs: a scripted model
# never notices that its input does not make sense.
# ---------------------------------------------------------------------------
async def test_reading_a_directory_says_so_rather_than_raising(rig) -> None:
    """"That is a directory, here is what is in it" beats an Errno 21 puzzle."""
    (rig.workspace / "src" / "sample.py").write_text("x = 1\n", encoding="utf-8")

    outcome = await invoke(rig, call("read_file", {"path": "src"}))

    assert outcome.is_error
    assert "is a directory" in outcome.content
    assert outcome.meta["is_directory"] is True
    assert "sample.py" in outcome.meta["entries"], "a listing is what makes it actionable"


async def test_an_unexecuted_command_is_reported_as_not_having_run(rig) -> None:
    """The recording sandbox must not let a command look like it succeeded.

    It answered ``exit_code=0`` with no output, which a model reads as "the
    command worked and printed nothing". Real models proceeded to reason about
    output that never existed.
    """
    rig.approval.set_answerer(lambda r: _allow())

    outcome = await invoke(rig, call("run", {"command": "echo hi"}))

    assert outcome.is_error
    assert outcome.meta["executed"] is False
    assert "NOT executed" in outcome.content
    assert rig.sandbox.calls, "it was still recorded — that is what a recording sandbox is for"
