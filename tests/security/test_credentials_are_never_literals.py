"""D2-4: a secret reaches a process **by name**, and never as a literal.

The credential channel is a mechanism only if the other path is closed. There are
exactly two ways a credential can get into a command:

* the model writes ``${NAME}``, and the value is substituted into the argv at the
  last moment — the value never enters the conversation, the event log or a tool
  result, which is the whole point;
* the model writes the value itself — refused in ``arm``, before a single
  capability is minted, because a channel a model can bypass by pasting the token
  into the command is a convention wearing a boundary's clothes.

**The refusal is deliberately not a redaction.** Redacting the literal would run
the command with a placeholder in it, fail somewhere further along, and tell the
model the credential had been used when in fact it never was. A wrong diagnosis
costs more than a refusal.

Four properties are asserted, and each is a way this could be quietly false:

1. a literal is refused, and nothing reaches the sandbox at all;
2. the refusal does not repeat the literal — a message the model then reasons
   about is the leak it is preventing;
3. a placeholder really is substituted (the recording sandbox saw the value) and
   really is scrubbed back out of the record (the meta argv and the durable log
   hold the placeholder);
4. a name that cannot be resolved is refused rather than run with an empty secret.
"""

from __future__ import annotations

import json

import pytest

from forgepilot.seams.approval import ApprovalOutcome
from forgepilot.seams.refs import SessionId, TurnId
from forgepilot.seams.session import EventKind
from forgepilot.seams.tools import ToolCall

#: A value shaped like a GitHub token. Not a credential, and it must never be
#: written down — which is the property under test.
LITERAL = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

#: The value behind the rig's declared name. Deliberately **not** secret-shaped:
#: no redaction rule would catch it, so anything that keeps it out of the log is
#: the credential channel rather than the gate.
VALUE = "s3cret-value-that-no-rule-would-catch"

#: Random-looking, and nothing else. The high-entropy rule is off by default and
#: is excluded from the refusal even when a deployment turns it on — "this looks
#: random" is not a defensible reason to refuse a command.
RANDOM_LOOKING = "Zx9Qw3Er7Ty1Up2As4Df6Gh8Jk0Lm5Nc"


def constraints_for(workspace):
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import ExecutionConstraints, Operation, Principal

    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        permitted_operations=frozenset(
            {Operation.READ_CODE, Operation.WRITE_CODE, Operation.RUN_TESTS}
        ),
        writable_paths=("src",),
    )


async def invoke(rig, command: str):
    """Drive one ``run`` call through the real pipeline.

    The answerer is set because approval comes **before** arm: without it the call
    would be refused for the wrong reason and every assertion below would pass
    while testing nothing.
    """
    rig.approval.set_answerer(lambda request: ApprovalOutcome.ALLOWED_ONCE)
    return await rig.pipeline.invoke(
        ToolCall(
            call_id="c-run",
            name="run",
            arguments_json=json.dumps({"command": command}),
        ),
        session=rig.session,
        turn=TurnId(1),
        step=1,
        constraints=constraints_for(rig.workspace),
    )


async def logged(rig) -> str:
    """The whole durable log, as text — the only thing that outlives the process."""
    await rig.session.flush()
    events = [event async for event in rig.store.read(rig.session.header.session_id)]
    return json.dumps([event.to_json() for event in events], ensure_ascii=False, default=str)


@pytest.fixture
async def echoing_rig(tmp_path):
    """A rig whose sandbox *reports output*, so a command can echo its own argv.

    The shared rig records without executing, which is right for "what would have
    run" and useless here: the interesting leak is the command printing the value
    it was given, and a sandbox that returns no output cannot produce one.
    """
    from datetime import UTC, datetime

    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber
    from forgepilot.runtime.guards import GuardRegistry, UsageTracker, default_guards
    from forgepilot.runtime.session import Session
    from forgepilot.runtime.tools.pipeline import ToolPipeline
    from forgepilot.seams.refs import PrincipalId, SessionId, TenantId
    from forgepilot.seams.security import Principal
    from forgepilot.seams.session import SessionHeader

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
    # `never` rather than an answerer: this file is about the credential boundary,
    # and a setup that also had to pass an approval gate would blur which one
    # refused a call.
    await load("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "never"})
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

    sid = SessionId("echoing")
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=sid, created_at=datetime.now(UTC), tenant="acme", principal="alice"
        )
    )

    try:
        yield type(
            "EchoingRig",
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


# ---------------------------------------------------------------------------
# The literal path, closed
# ---------------------------------------------------------------------------
async def test_a_secret_shaped_literal_is_refused_before_anything_is_armed(rig) -> None:
    outcome = await invoke(
        rig, f"git push https://x-access-token:{LITERAL}@github.com/acme/svc.git"
    )

    assert outcome.is_error, "a command carrying a live-looking credential must not run"
    assert outcome.meta.get("refused") is True
    assert outcome.meta["phase"] == "arm", "the boundary is arm; nothing later may see it"
    assert rig.sandbox.calls == [], "the command reached the sandbox anyway"


async def test_the_refusal_says_what_to_do_and_does_not_repeat_the_secret(rig) -> None:
    outcome = await invoke(rig, f"curl -H 'Authorization: token {LITERAL}' https://api.x/y")

    # Worded to be acted on: the mechanism is named, with an example.
    assert "credential channel" in outcome.content
    assert "${" in outcome.content
    # And the value is not echoed back into a result the model will reason about.
    assert LITERAL not in outcome.content
    assert "ghp_ABC" not in outcome.content


async def test_the_log_of_a_refused_call_does_not_hold_the_literal(rig) -> None:
    """The tool call itself carries the literal; the gate is what keeps it out.

    Two mechanisms are interlocking here. ``arm`` refuses so the value cannot
    reach a process, and ``Session.append`` redacts so the *attempt* — which the
    model wrote into its own arguments — is not preserved either.
    """
    await invoke(rig, f"deploy --token {LITERAL}")

    dump = await logged(rig)

    assert LITERAL not in dump
    # A token-classified payload is withheld whole (plan §零 line 139): every
    # string becomes a stub, and the *keys* survive so the log still has the shape
    # of a tool call.
    assert "«redacted:secret-event:" in dump
    assert '"callId"' in dump, "withholding must not destroy the event's shape"
    assert '"kind": "token"' in dump, "the audit records which rule fired"


async def test_a_random_looking_string_is_not_refused(rig) -> None:
    """Refusing on shape alone would refuse working commands.

    The high-entropy rule is the one detector excluded from the literal check: the
    other six match a *named* credential shape, which is evidence, and this one
    matches "unusual", which is a guess.
    """
    outcome = await invoke(rig, f"echo {RANDOM_LOOKING}")

    assert outcome.meta.get("refused") is not True
    assert rig.sandbox.calls, "the command should have reached the sandbox"
    assert RANDOM_LOOKING in rig.sandbox.calls[-1].argv[-1]


# ---------------------------------------------------------------------------
# The named path, open — and still not written down
# ---------------------------------------------------------------------------
async def test_a_placeholder_reaches_the_process_and_not_the_record(rig, monkeypatch) -> None:
    monkeypatch.setenv("FORGEPILOT_TEST_TOKEN", VALUE)

    outcome = await invoke(rig, "deploy --token ${demo_token}")

    # It really was substituted: the sandbox is the only place the value may exist.
    assert rig.sandbox.calls, "the command never reached the sandbox"
    assert VALUE in rig.sandbox.calls[-1].argv[-1]

    # And it was put back before anything was recorded — the meta argv is the
    # *substituted* argv, which is why the scrub is not optional.
    assert outcome.meta["secrets"] == ["demo_token"]
    assert VALUE not in json.dumps(outcome.meta, default=str)
    assert "${demo_token}" in json.dumps(outcome.meta, default=str)

    dump = await logged(rig)
    assert VALUE not in dump
    assert "${demo_token}" in dump


async def test_the_claim_is_audited_by_name_only(rig, monkeypatch) -> None:
    """A name is auditable; a value is the thing the audit must not contain."""
    monkeypatch.setenv("FORGEPILOT_TEST_TOKEN", VALUE)

    await invoke(rig, "deploy --token ${demo_token}")

    await rig.session.flush()
    events = [event async for event in rig.store.read(SessionId("s-under-test"))]
    claims = [event for event in events if event.kind is EventKind.CREDENTIAL_CLAIMED]

    assert len(claims) == 1
    assert claims[0].data == {"names": ["demo_token"]}


async def test_a_name_nobody_declared_is_refused_rather_than_run_empty(rig) -> None:
    outcome = await invoke(rig, "deploy --token ${nope}")

    assert outcome.meta.get("refused") is True
    assert outcome.meta["phase"] == "arm"
    assert "nope" in outcome.content
    assert rig.sandbox.calls == []


async def test_with_no_channel_at_all_the_command_is_refused(rig, monkeypatch) -> None:
    """The absence of a channel is a refusal, not a licence to run the placeholder.

    Leaving ``${demo_token}`` in the command would hand the shell a literal string
    that looks like a credential, fails authentication, and reads as a broken
    secret rather than an unconfigured deployment.
    """
    monkeypatch.setenv("FORGEPILOT_TEST_TOKEN", VALUE)
    rig.pipeline.credentials = None

    outcome = await invoke(rig, "deploy --token ${demo_token}")

    assert outcome.meta.get("refused") is True
    assert "no credential channel is configured" in outcome.content
    assert rig.sandbox.calls == []


async def test_a_command_that_prints_the_value_does_not_write_it_down(
    echoing_rig, monkeypatch
) -> None:
    """Mode B's promise, at the place it is easiest to break.

    ``echo ${demo_token}`` means the value comes back as the command's *own
    output* — which becomes the tool result, which becomes a durable event. So the
    scrub has to cover the output and not only the argv meta, and the redaction
    gate cannot be relied on for it: ``VALUE`` is deliberately not secret-shaped,
    which is precisely why a channel exists instead of a rule.
    """
    from forgepilot.providers.sandbox.scripted import ScriptedResult

    monkeypatch.setenv("FORGEPILOT_TEST_TOKEN", VALUE)
    echoing_rig.sandbox.script(ScriptedResult(exit_code=0, stdout=f"echoed: {VALUE}\n"))

    outcome = await echoing_rig.pipeline.invoke(
        ToolCall(
            call_id="c-run",
            name="run",
            arguments_json=json.dumps({"command": "echo ${demo_token}"}),
        ),
        session=echoing_rig.session,
        turn=TurnId(1),
        step=1,
        constraints=constraints_for(echoing_rig.workspace),
    )

    assert VALUE in echoing_rig.sandbox.calls[-1][-1], "the substitution must be real"
    assert VALUE not in outcome.content
    assert "${demo_token}" in outcome.content

    dump = await logged(echoing_rig)
    assert VALUE not in dump


# ---------------------------------------------------------------------------
# The seam helpers, which the two ends must agree on
# ---------------------------------------------------------------------------
def test_placeholders_are_found_in_first_use_order_and_deduplicated() -> None:
    from forgepilot.seams.credentials import placeholder, placeholders

    text = "use ${b} then ${a} then ${b} again"

    assert placeholders(text) == ("b", "a")
    assert placeholder("a") == "${a}"
    assert placeholders("nothing here") == ()


def test_scrub_values_puts_every_value_back() -> None:
    """The value-aware layer, which catches what no pattern would.

    It is the reason the tool returns what it substituted: a rule set is a guess
    about shape, and this is exact.
    """
    from forgepilot.runtime.credentials import scrub_values

    # The mapping is *name -> value*, the same shape `resolve_placeholders`
    # returns: what the tool substituted, so it can put it back.
    assert (
        scrub_values("curl -H 'x: abc' https://h/y", {"demo_token": "abc"})
        == "curl -H 'x: ${demo_token}' https://h/y"
    )
    assert scrub_values("untouched", {}) == "untouched"


async def test_a_literal_is_recognised_only_by_the_named_rules() -> None:
    """``literal_secret`` excludes high-entropy *even when it is enabled*."""
    from forgepilot.runtime.credentials import literal_secret
    from forgepilot.runtime.redaction import Redactor

    redactor = Redactor(high_entropy=True)

    found = literal_secret(f"token {LITERAL}", redactor)
    assert found is not None
    assert found.kind != "high-entropy"

    assert literal_secret(f"echo {RANDOM_LOOKING}", redactor) is None
