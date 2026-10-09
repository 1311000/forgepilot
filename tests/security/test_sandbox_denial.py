"""Telling "the sandbox refused" apart from "the command failed".

The distinction is not cosmetic. Codex issue #41320 documents what happens when
it is lost: the sandbox denial is stripped from the model-facing result, the model
sees only an exit code, and it responds by looking for *another execution
environment* instead of taking the escalation path it was given. The agent flails
because it cannot tell a boundary from a bug.

So this classifier exists, and it is deliberately conservative: it names the
signal it matched, and it says "likely" because a heuristic that claims certainty
would be worse than no heuristic at all — `permission denied` is also what a
command says when a file is simply not yours.
"""

from __future__ import annotations

import pytest


@pytest.fixture
async def scripted_rig(tmp_path):
    """A rig whose sandbox answers from a script, so output can be arranged.

    The shared `rig` uses the recording sandbox, which returns no output at all —
    fine for asserting what *would* have run, useless for asserting what the
    model is told about how it failed.
    """
    from datetime import datetime, timezone

    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber
    from forgepilot.runtime.guards import GuardRegistry, UsageTracker, default_guards
    from forgepilot.runtime.session import Session
    from forgepilot.runtime.tools.pipeline import ToolPipeline
    from forgepilot.seams.refs import SessionId
    from forgepilot.seams.session import SessionHeader

    root = create_root()
    fibers = []

    async def load(spec, config=None):
        fiber = await root.plugin(resolve_plugin(spec), config or {})
        fibers.append(fiber)

    await load("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin")
    await load("forgepilot.providers.sandbox.scripted:ScriptedSandboxPlugin", {"enforcement": "full"})
    await load("forgepilot.runtime.tools.registry:ToolRegistryPlugin")
    await load("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "never"})
    await load("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin")

    tracker = UsageTracker()
    guards = GuardRegistry()
    for name, guard in default_guards(tracker):
        guards.register(name, guard)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    pipeline = ToolPipeline(
        registry=root.tools,
        guards=guards,
        approval=root.approval,
        enforcement=root.sandbox.probe,
        workspace_root=workspace,
    )
    root.tools.attach_pipeline(pipeline)

    sid = SessionId("denial")
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=sid, created_at=datetime.now(timezone.utc), tenant="acme", principal="alice"
        )
    )

    try:
        yield type(
            "Rig", (), {"root": root, "sandbox": root.sandbox, "pipeline": pipeline,
                        "session": await Session.open(root.sessionPersistence, sid)}
        )
    finally:
        for fiber in reversed(fibers):
            await dispose_fiber(fiber)


def constraints_for(*operations):
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import (
        ExecutionConstraints,
        NetworkAccess,
        Principal,
    )

    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        permitted_operations=frozenset(operations),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


def result(exit_code: int, stderr: str = "", stdout: str = "", executed: bool = True):
    from forgepilot.seams.sandbox import ExecResult, SandboxEnforcement

    return ExecResult(
        argv=("pytest",),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        enforcement=SandboxEnforcement.FULL,
        executed=executed,
    )


# ---------------------------------------------------------------------------
# It finds the boundary
# ---------------------------------------------------------------------------
def test_a_syscall_killed_by_the_filter_is_named() -> None:
    """SIGSYS is the one signal seccomp gives that nothing else gives.

    A process killed by an unhandled signal reports 128 + signum, and SIGSYS is
    31 — so 159 means a syscall was blocked, which only a sandbox does.
    """
    from forgepilot.runtime.sandbox_denial import likely_denial

    reason = likely_denial(result(128 + 31, stderr="Bad system call"))

    assert reason
    assert "syscall" in reason


def test_a_read_only_filesystem_is_named() -> None:
    from forgepilot.runtime.sandbox_denial import likely_denial

    reason = likely_denial(result(1, stderr="sh: can't create /etc/x: Read-only file system"))

    assert reason
    assert "read-only" in reason.lower()


def test_a_refused_permission_is_named() -> None:
    from forgepilot.runtime.sandbox_denial import likely_denial

    assert likely_denial(result(1, stderr="curl: (7) Failed to connect: Operation not permitted"))


# ---------------------------------------------------------------------------
# And it does not cry wolf
# ---------------------------------------------------------------------------
def test_an_ordinary_failing_test_is_not_a_denial() -> None:
    """The common case, and the one that must stay quiet: a red suite is a red
    suite, not the sandbox."""
    from forgepilot.runtime.sandbox_denial import likely_denial

    failing = result(
        1,
        stdout="1 failed, 3 passed\nassert retry_login(None) is None",
    )

    assert likely_denial(failing) == ""


def test_a_missing_binary_is_not_a_denial() -> None:
    """127 is ambiguous by itself: `command not found` and a sandbox can both
    produce it. Without a signal that only a boundary gives, it is not a denial."""
    from forgepilot.runtime.sandbox_denial import likely_denial

    assert likely_denial(result(127, stderr="pytest: command not found")) == ""


def test_a_command_that_never_ran_is_not_a_denial_it_is_an_absence() -> None:
    """`executed=False` already says "nothing happened", and the provider's own
    stderr says why. Adding a denial guess on top would blur the one case that
    is *certain* into the one that is guessed."""
    from forgepilot.runtime.sandbox_denial import likely_denial

    assert likely_denial(result(126, stderr="refusing to run uncontained", executed=False)) == ""


def test_success_is_never_a_denial() -> None:
    from forgepilot.runtime.sandbox_denial import likely_denial

    assert likely_denial(result(0, stderr="warning: operation not permitted")) == ""


# ---------------------------------------------------------------------------
# It reaches the model
# ---------------------------------------------------------------------------
async def test_the_run_tool_tells_the_model_the_sandbox_was_the_boundary(scripted_rig) -> None:
    """The whole point: the agent must be able to tell "blocked" from "broken".

    Without this the model sees a failing command and starts trying other things
    — which is the reported Codex failure mode, arrived at from the other end.
    """
    import json

    from forgepilot.seams.security import Operation
    from forgepilot.seams.tools import ToolCall

    scripted_rig.sandbox.script(
        __import__(
            "forgepilot.providers.sandbox.scripted", fromlist=["ScriptedResult"]
        ).ScriptedResult(exit_code=1, stderr="touch: cannot touch '/etc/x': Read-only file system")
    )

    outcome = await scripted_rig.pipeline.invoke(
        ToolCall(
            call_id="c1",
            name="run",
            arguments_json=json.dumps({"command": "touch /etc/x"}),
        ),
        session=scripted_rig.session,
        turn=1,
        step=1,
        constraints=constraints_for(Operation.RUN_TESTS),
    )

    assert outcome.is_error
    assert "sandbox" in outcome.content.lower(), (
        "the model was told the command failed but not that a boundary refused it"
    )
