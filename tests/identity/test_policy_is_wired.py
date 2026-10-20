"""The decision authority is actually consulted.

`PolicyService.decide()` was written, documented as *"the only decision authority
in the system"*, tested to the letter — and called by nothing in ``src/``. The
shipped `policy` row's `default: deny` and `ask_operations: [open_pr]` were read
by a function no code path reached, so "opening a pull request always needs a
human" was a sentence in a config file and nothing more.

Every test here fails if that regresses, and the first one fails if the *config*
regresses — which is the more likely way, because `deny` looks deliberate.

⚠️ **The `default: deny` trap.** Granting is not this service's job. It never
sees the grant — that is `ExecutionConstraints`, narrowed by `derive` and
enforced by the guards and `ToolDefinition.required_operation`. A `deny` default
here means **every tool call refused**, because `read_file` maps to `read_code`,
which nobody listed. It went unnoticed only because nothing called `decide()`.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from forgepilot.identity.policy import PolicyDecision
from forgepilot.seams.refs import PrincipalId, SessionId, TenantId, TurnId
from forgepilot.seams.security import (
    ExecutionConstraints,
    NetworkAccess,
    Operation,
    Principal,
)
from forgepilot.seams.tools import ToolCall

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPO_ROOT / "configs"

PRINCIPAL = Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme"))


def constraints() -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        PRINCIPAL,
        permitted_operations=frozenset(Operation),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


async def build_root(policy_config: dict, **extra):
    """A container with just enough for a session: log, registry, approval, tools."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root

    root = create_root()
    fibers = []
    loaded: dict[str, object] = {"fibers": fibers}

    async def load(spec: str, config: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), config or {})
        fibers.append(fiber)
        return fiber

    await load("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin")
    await load(
        "forgepilot.providers.sandbox.recording:RecordingSandboxPlugin",
        {"enforcement": "full"},
    )
    await load("forgepilot.runtime.tools.registry:ToolRegistryPlugin")
    await load("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "ask"})
    await load("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin")
    await load("forgepilot.identity.policy:PolicyServicePlugin", policy_config)
    for spec, config in extra.items():
        loaded[spec] = await load(spec, config)
    loaded["root"] = root
    return loaded


async def open_session(root, tmp_path: Path):
    from forgepilot.runtime.assembly import assemble_session

    return await assemble_session(root, constraints(), workspace_root=tmp_path, principal=PRINCIPAL)


async def call_read(runtime, tmp_path: Path):
    return await runtime.pipeline.invoke(
        ToolCall(call_id="c1", name="read_file", arguments_json='{"path": "src/app.py"}'),
        session=runtime.session,
        turn=TurnId(1),
        step=1,
        constraints=constraints(),
    )


# ---------------------------------------------------------------------------
# The shipped configuration
# ---------------------------------------------------------------------------
async def test_the_shipped_profile_does_not_refuse_every_tool_call() -> None:
    """**The trap, guarded.** `default: deny` refuses `read_file`.

    Asserted *behaviourally*, against the resolved profile's own config, not by
    reading the YAML: the point is what `decide` returns for an ordinary
    operation, and a test that greps for the word "allow" would pass if the key
    were misspelled.
    """
    from forgepilot.config import apply_layers, compose_layers, load_profile, resolve_plugin

    profile = load_profile(CONFIGS, "default")
    resolved = apply_layers(compose_layers(profile, []), profile=profile.name)
    row = resolved.row("policy")

    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(resolve_plugin(row.plugin), row.config)
    try:
        policy = root.policy
        assert policy.decide(PRINCIPAL, Operation.READ_CODE) is PolicyDecision.ALLOW, (
            "the shipped profile denies an ordinary read. Granting is not policy's "
            "job — it never sees the grant — so a `deny` default here refuses every "
            "tool call there is."
        )
        assert policy.decide(PRINCIPAL, Operation.WRITE_CODE) is PolicyDecision.ALLOW
        # And the checkpoint it exists for is still a checkpoint.
        assert policy.decide(PRINCIPAL, Operation.OPEN_PR) is PolicyDecision.ASK
    finally:
        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# Reaching the pipeline
# ---------------------------------------------------------------------------
async def test_a_denial_refuses_the_call_before_anything_runs(tmp_path: Path) -> None:
    """`decide` returning DENY has to become a refusal, or it decides nothing."""
    loaded = await build_root({"default": "deny"})
    root = loaded["root"]
    try:
        runtime = await open_session(root, tmp_path)
        outcome = await call_read(runtime, tmp_path)

        assert outcome.is_error
        assert outcome.meta["phase"] == "pre-execute"
        assert "policy" in outcome.content
        await runtime.close()
    finally:
        from forgepilot.kernel import dispose_fiber

        for fiber in reversed(loaded["fibers"]):
            await dispose_fiber(fiber)


async def test_an_ask_reaches_the_approval_phase(tmp_path: Path) -> None:
    """`ASK` is not a decision — it hands the question to a person.

    The tool here *would* succeed; it is refused only because nobody answered.
    That is the fail-closed property, and it is what makes `ask_operations`
    mean something.
    """
    loaded = await build_root({"default": "allow", "ask_operations": ["read_code"]})
    root = loaded["root"]
    try:
        runtime = await open_session(root, tmp_path)
        outcome = await call_read(runtime, tmp_path)

        assert outcome.is_error
        assert outcome.meta["phase"] == "approval", (
            "the call was not escalated, so `ask_operations` had no effect"
        )
        assert "approval" in outcome.content
        # And it is in the log, which is how a human would ever learn to answer.
        from forgepilot.seams.session import EventKind

        kinds = [str(e.kind) for e in runtime.session.events]
        assert EventKind.APPROVAL_ASKED in kinds, kinds
        await runtime.close()
    finally:
        from forgepilot.kernel import dispose_fiber

        for fiber in reversed(loaded["fibers"]):
            await dispose_fiber(fiber)


async def test_an_allowed_call_still_runs(tmp_path: Path) -> None:
    """The other direction, so the two above cannot pass by refusing everything."""
    loaded = await build_root({"default": "allow"})
    root = loaded["root"]
    try:
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        runtime = await open_session(root, tmp_path)
        outcome = await call_read(runtime, tmp_path)

        assert not outcome.is_error, outcome.content
        assert "def main" in outcome.content
        await runtime.close()
    finally:
        from forgepilot.kernel import dispose_fiber

        for fiber in reversed(loaded["fibers"]):
            await dispose_fiber(fiber)


async def test_policy_abstains_on_allow_so_other_listeners_still_run(tmp_path: Path) -> None:
    """⚠️ **The mistake this test exists for.**

    The chain is *first opinion wins* and the policy listener is registered
    first. Returning `ALLOW` on an ordinary call therefore **ends the chain**,
    and every other pre-execute listener in the process becomes dead code — which
    is exactly what happened, and how it was noticed: listeners that tests
    registered stopped being called.

    Policy's `allow` means "I do not object". A listener that does not object
    must not be the last word.
    """
    loaded = await build_root({"default": "allow"})
    root = loaded["root"]
    try:
        runtime = await open_session(root, tmp_path)

        from forgepilot.runtime.tools.pre_execute import PreDecision, PreDecisionKind

        seen: list[str] = []

        def later_listener(preview, scope):  # noqa: ANN001
            seen.append(preview.tool)
            return PreDecision(kind=PreDecisionKind.DENY, reason="a later listener objected")

        runtime.pipeline.on_pre_execute(later_listener)

        outcome = await call_read(runtime, tmp_path)

        assert seen == ["read_file"], (
            "policy returned ALLOW and ended the chain, so no later listener was "
            "consulted — every pre-execute hook in the process just stopped running"
        )
        assert outcome.is_error
        assert "a later listener objected" in outcome.content
        await runtime.close()
    finally:
        from forgepilot.kernel import dispose_fiber

        for fiber in reversed(loaded["fibers"]):
            await dispose_fiber(fiber)


async def test_a_pipeline_without_a_principal_refuses_rather_than_skipping(
    tmp_path: Path,
) -> None:
    """A permission check that cannot say *who* cannot check anything.

    Skipping would be the wrong way round for the one phase whose whole job is to
    decide, so a pipeline with no principal refuses every call by name.
    """
    from forgepilot.runtime.tools.pipeline import ToolPipeline

    loaded = await build_root({"default": "allow"})
    root = loaded["root"]
    try:
        from datetime import datetime

        from forgepilot.runtime.session import Session
        from forgepilot.seams.session import SessionHeader

        sid = SessionId("no-principal")
        await root.sessionPersistence.create(
            SessionHeader(
                session_id=sid,
                created_at=datetime.now(UTC),
                tenant="acme",
                principal="alice",
            )
        )
        session = await Session.open(root.sessionPersistence, sid)
        pipeline = ToolPipeline(
            registry=root.tools,
            guards=__import__(
                "forgepilot.runtime.guards", fromlist=["GuardRegistry"]
            ).GuardRegistry(),
            approval=root.approval,
            enforcement=root.sandbox.probe,
            workspace_root=tmp_path,
        )
        root.tools.attach_pipeline(pipeline)

        outcome = await pipeline.invoke(
            ToolCall(call_id="c1", name="read_file", arguments_json='{"path": "a.py"}'),
            session=session,
            turn=TurnId(1),
            step=1,
            constraints=constraints(),
        )

        assert outcome.is_error
        assert "no principal is attached" in outcome.content
    finally:
        from forgepilot.kernel import dispose_fiber

        for fiber in reversed(loaded["fibers"]):
            await dispose_fiber(fiber)
