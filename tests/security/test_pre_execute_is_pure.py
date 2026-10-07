"""Regression test for the defect that shaped the whole pipeline.

DeepSeek Harness discussion #1863 documents this: a ``tools/pre-execute``
listener is ordinary plugin code with ambient host privileges, and it must run to
completion *in order to* return ``ask``. So it can spawn a process, let it exit,
and only then raise the approval prompt. A subsequent "deny" cannot undo what
already ran. The reported proof of concept spawned a Node child process, waited
for ``code=0``, and *then* returned ``ask``.

**This file must never be deleted.** It is the executable form of that defect. If
a future change makes one of these assertions impossible to satisfy, the change
has reintroduced the vulnerability rather than made the test obsolete.

What is asserted, and why each one matters:

1. the pre-execute scope holds *nothing that can act*;
2. it cannot be extended at runtime;
3. the ordering guarantee — nothing privileged runs before approval — is read off
   the phase trace rather than assumed;
4. **a denial means no effect occurred**, which is precisely the guarantee DSH
   could not give.

Honest limit, stated rather than glossed: in-process Python cannot give a plugin
a real capability boundary — ``import subprocess`` still works inside a listener.
(1) and (2) narrow what a listener is *handed* and make the mistake path loud.
The boundary that contains hostile code is running third-party plugins out of
process, which is a separate decision already made. This file does not pretend
otherwise.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from forgepilot.runtime.tools.arm import ArmRefused, ArmScope, mint_capabilities
from forgepilot.runtime.tools.pre_execute import (
    PreDecision,
    PreDecisionKind,
    PreExecuteScope,
)
from forgepilot.seams.approval import ApprovalOutcome
from forgepilot.seams.refs import PrincipalId, TenantId, TurnId
from forgepilot.seams.sandbox import SandboxEnforcement
from forgepilot.seams.security import ExecutionConstraints, Principal
from forgepilot.seams.tools import ToolCall, ToolEffect

#: Names a hook might reach for if it wanted to cause an effect. The point of the
#: scope is that none of them is there.
FORBIDDEN_ON_SCOPE = (
    "sandbox",
    "workspace",
    "session",
    "ctx",
    "context",
    "open",
    "subprocess",
    "socket",
    "httpx",
    "requests",
    "os",
    "shutil",
    "pathlib",
    "capabilities",
    "run",
    "execute",
)


def constraints_for(workspace):
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        writable_paths=("src",),
    )


def make_call(name: str, arguments: dict) -> ToolCall:
    return ToolCall(call_id=f"c-{name}", name=name, arguments_json=json.dumps(arguments))


async def deny_always(request) -> ApprovalOutcome:
    return ApprovalOutcome.REJECTED


# ---------------------------------------------------------------------------
# 1 & 2: the scope holds nothing, and cannot gain anything
# ---------------------------------------------------------------------------
def test_scope_holds_nothing_that_can_act(rig) -> None:
    """A hook is handed a logger and constraints. That is the entire world."""
    scope = PreExecuteScope(logger=None, constraints=constraints_for(rig.workspace))

    field_names = {f.name for f in dataclasses.fields(scope)}
    assert field_names == {"logger", "constraints"}, (
        "PreExecuteScope gained a field. If it is something a hook could act "
        "through, the #1863 protection is gone — privileged preparation belongs in "
        "runtime/tools/arm.py, which runs after approval."
    )
    for name in FORBIDDEN_ON_SCOPE:
        assert not hasattr(scope, name), f"pre-execute scope exposes {name!r}"


def test_scope_is_frozen_and_slotted(rig) -> None:
    """It cannot be extended at runtime either."""
    scope = PreExecuteScope(logger=None, constraints=constraints_for(rig.workspace))

    with pytest.raises(AttributeError):
        scope.sandbox = object()  # type: ignore[misc]

    assert not hasattr(scope, "__dict__"), "slots must be in force, or attributes can be added"


# ---------------------------------------------------------------------------
# 3: ordering
# ---------------------------------------------------------------------------
async def test_nothing_privileged_runs_before_approval(rig) -> None:
    rig.approval.set_answerer(deny_always)
    rig.pipeline.on_pre_execute(
        lambda preview, scope: PreDecision(PreDecisionKind.ASK, reason="needs a human")
    )

    outcome = await rig.pipeline.invoke(
        make_call("run", {"command": "rm -rf /"}),
        session=rig.session,
        turn=TurnId(1),
        step=1,
        constraints=constraints_for(rig.workspace),
    )

    trace = rig.pipeline.trace
    assert "approval" in trace and trace.index("approval") < trace.index("tool/result")
    assert "arm" not in trace, "arm must not run when approval was refused"
    assert "execute" not in trace
    assert outcome.is_error and outcome.meta.get("refused") is True


# ---------------------------------------------------------------------------
# 4: a refusal really means nothing happened
# ---------------------------------------------------------------------------
async def test_a_denied_call_produces_no_effect_at_all(rig) -> None:
    """The guarantee DSH could not give: refusal means nothing happened.

    There is no window in which a hook could already have done the work, because
    the only phase able to reach the sandbox runs after the refusal.
    """
    rig.approval.set_answerer(deny_always)
    rig.pipeline.on_pre_execute(
        lambda preview, scope: PreDecision(PreDecisionKind.ASK, reason="needs a human")
    )

    await rig.pipeline.invoke(
        make_call("run", {"command": "echo should-not-run"}),
        session=rig.session,
        turn=TurnId(1),
        step=1,
        constraints=constraints_for(rig.workspace),
    )

    assert rig.sandbox.calls == [], (
        "the sandbox was reached for a call the human refused. This is exactly the "
        "DSH #1863 failure mode."
    )
    assert rig.approval.asked, "the prompt was raised"
    assert rig.approval.decided[-1][1] is not ApprovalOutcome.ALLOWED_ONCE


async def test_a_hook_reaching_for_an_effect_finds_nothing(rig) -> None:
    """The mistake path is loud: AttributeError, not a silently acquired handle.

    This does not contain hostile code — nothing in-process could. It ensures a
    hook author who *assumes* a sandbox is available is told immediately.
    """
    attempts: list[str] = []

    def greedy_hook(preview, scope):
        for name in ("sandbox", "workspace", "session", "subprocess", "open"):
            with pytest.raises(AttributeError):
                getattr(scope, name)
            attempts.append(name)
        return PreDecision(PreDecisionKind.DENY, reason="found nothing to act with")

    rig.pipeline.on_pre_execute(greedy_hook)

    outcome = await rig.pipeline.invoke(
        make_call("run", {"command": "echo hi"}),
        session=rig.session,
        turn=TurnId(1),
        step=1,
        constraints=constraints_for(rig.workspace),
    )

    assert attempts == ["sandbox", "workspace", "session", "subprocess", "open"]
    assert outcome.is_error
    assert rig.sandbox.calls == []


# ---------------------------------------------------------------------------
# The complementary rule: an unenforceable sandbox is not a sandbox
# ---------------------------------------------------------------------------
def test_exec_is_refused_when_the_host_cannot_contain_it(rig) -> None:
    """Arm mints capabilities from what the host can actually enforce.

    A host reporting less than ``full`` refuses exec-capable tools outright rather
    than running them uncontained and hoping. On Windows the ACL restricted-token
    backend honestly reports ``partial``, so shell tools refuse there — which is
    the correct outcome, not a gap to paper over.
    """
    scope = ArmScope(
        session_id=rig.session.header.session_id,
        constraints=constraints_for(rig.workspace),
        enforcement=SandboxEnforcement.PARTIAL,
    )

    with pytest.raises(ArmRefused, match="weaker than the required"):
        mint_capabilities(effects=frozenset({ToolEffect.EXEC}), scope=scope)

    # Lowering the requirement explicitly is allowed, and visible at the call site.
    granted = mint_capabilities(
        effects=frozenset({ToolEffect.EXEC}),
        scope=scope,
        minimum_enforcement_for_exec=SandboxEnforcement.PARTIAL,
    )
    assert granted.exec is True
