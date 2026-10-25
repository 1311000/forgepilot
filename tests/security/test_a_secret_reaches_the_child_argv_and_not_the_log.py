"""What the `${NAME}` channel does and does not keep out of the workload.

The threat model used to say "a credential never appears as a literal — the value lives in
the environment". Half of that was wrong, and the way it was wrong matters: the value is
resolved **in this process** and substituted into the command string, so while the command
runs it is in the child's `argv`. What is true — and what this file pins — is the narrower
claim the mechanism actually supports:

* the value reaches the process (that is the feature; a channel that did not would not run
  the command), and
* nothing that is recorded holds it: not the tool result, not the durable log, because the
  substitution returns the values so they can be scrubbed back out.

Names are the boundary the model sees; the literal is only ever a temporary guest in a
process's argv. `docs/notes/boundary-comparison.md` is where that difference is written down
next to a design that keeps the credential outside the workload entirely.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from forgepilot.config import resolve_plugin
from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.runtime.guards import GuardRegistry, UsageTracker, default_guards
from forgepilot.runtime.session import Session
from forgepilot.runtime.tools.pipeline import ToolPipeline
from forgepilot.seams.approval import ApprovalOutcome
from forgepilot.seams.refs import PrincipalId, SessionId, TenantId, TurnId
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal
from forgepilot.seams.session import SessionHeader
from forgepilot.seams.tools import ToolCall

#: The value behind the rig's declared name. Deliberately **not** secret-shaped: no redaction
#: rule would catch it, so anything that keeps it out of the log is the credential channel
#: rather than the gate.
VALUE = "s3cret-value-that-no-rule-would-catch"


def _constraints() -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        permitted_operations=frozenset(
            {Operation.READ_CODE, Operation.WRITE_CODE, Operation.RUN_TESTS}
        ),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


@pytest.fixture
async def credential_rig(tmp_path, monkeypatch):
    """The real pipeline, a recording sandbox, and one declared `${demo_token}`."""
    monkeypatch.setenv("FORGEPILOT_TEST_TOKEN", VALUE)
    root = create_root()
    fibers = []

    async def load(spec, config=None):
        fiber = await root.plugin(resolve_plugin(spec), config or {})
        fibers.append(fiber)

    await load("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin")
    await load(
        "forgepilot.providers.sandbox.scripted:ScriptedSandboxPlugin", {"enforcement": "full"}
    )
    await load("forgepilot.runtime.tools.registry:ToolRegistryPlugin")
    await load("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "ask"})
    await load("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin")
    await load(
        "forgepilot.providers.credentials.env:EnvCredentialPlugin",
        {"entries": {"demo_token": {"env": "FORGEPILOT_TEST_TOKEN"}}},
    )

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
        principal=Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        workspace_root=workspace,
        credentials=root.credentials,
    )
    root.tools.attach_pipeline(pipeline)
    root.approval.set_answerer(lambda request: ApprovalOutcome.ALLOWED_ONCE)

    sid = SessionId("credential-argv")
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=sid, created_at=datetime.now(UTC), tenant="acme", principal="alice"
        )
    )
    try:
        yield type(
            "CredentialRig",
            (),
            {
                "root": root,
                "sandbox": root.sandbox,
                "pipeline": pipeline,
                "session": await Session.open(root.sessionPersistence, sid),
                "store": root.sessionPersistence,
                "workspace": workspace,
            },
        )
    finally:
        for fiber in reversed(fibers):
            await dispose_fiber(fiber)


async def _run(rig, command: str):
    return await rig.pipeline.invoke(
        ToolCall(call_id="c-run", name="run", arguments_json=json.dumps({"command": command})),
        session=rig.session,
        turn=TurnId(1),
        step=1,
        constraints=_constraints(),
    )


async def _logged(rig) -> str:
    await rig.session.flush()
    events = [event async for event in rig.store.read(rig.session.header.session_id)]
    return json.dumps([event.to_json() for event in events], ensure_ascii=False, default=str)


@pytest.mark.asyncio
async def test_the_value_is_in_the_child_argv(credential_rig) -> None:
    outcome = await _run(credential_rig, "echo ${demo_token}")
    assert not outcome.is_error, outcome.content
    recorded = credential_rig.sandbox.calls[-1]
    assert VALUE in " ".join(recorded), (
        "the substitution did not reach the process — then the channel is not doing its job"
    )


@pytest.mark.asyncio
async def test_neither_the_result_nor_the_log_holds_the_value(credential_rig) -> None:
    outcome = await _run(credential_rig, "echo ${demo_token}")

    assert VALUE not in outcome.content
    events = await _logged(credential_rig)
    assert VALUE not in events
    assert "${demo_token}" in events, (
        "the log should hold the name the caller wrote, so a reader can tell what ran"
    )
