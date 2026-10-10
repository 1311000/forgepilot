"""Authorising the harness's *own* privileged acts.

The tool pipeline answers this question for anything the **model** asks for: a
call goes through ``tools/pre-execute``, which asks the policy service, which can
say allow, ask or deny. Every outward-facing thing the **harness** does bypasses
that entirely, because it is not a tool call — there is no ``tool/call`` event, no
armed capability, no ``describe_targets``. ``run_task`` pushes a branch and opens
a pull request by calling the seam directly.

So `ask_operations: [open_pr]` was a sentence in a config file and nothing more:
the operation the configuration is *about* is precisely the one that never
reaches the mechanism the configuration drives. This module is where that gap is
closed.

**It is not a second decision authority.** It calls the same
:meth:`PolicyServicePlugin.decide` the pipeline's listener calls, and maps the
same three outcomes. What it adds is the `ask` half, which the pipeline gets from
its approval phase and which has no equivalent outside it.

The fail-closed rules are the pipeline's, verbatim, because they are the ones
that matter:

- ``ALLOWED_ONCE`` is the only permissive approval outcome. Everything else —
  including *nobody answered* — refuses.
- **The prompt is flushed before the answerer is awaited.** The answer arrives on
  a different channel that reads the *log*, so a buffered prompt is invisible,
  nobody answers, and the run hangs with no error rather than failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forgepilot.seams.approval import ApprovalOutcome, ApprovalRequest
from forgepilot.seams.security import Operation, Principal
from forgepilot.seams.session import EventKind

__all__ = ["Authorisation", "authorise"]


@dataclass(frozen=True, slots=True)
class Authorisation:
    """Whether a harness action may proceed, and why not when it may not."""

    allowed: bool
    reason: str = ""
    #: True when a human was asked, so a caller can tell "policy refused" from
    #: "policy escalated and the escalation failed" — two different reports.
    asked: bool = False


async def authorise(
    *,
    policy: Any,
    approval: Any,
    principal: Principal,
    operation: Operation,
    session: Any,
    summary: str,
) -> Authorisation:
    """Decide a harness-driven action, asking a human when policy says to.

    ``summary`` is what a person sees in the prompt. It is written by the harness
    from what actually happened, never by the model — an approval request that a
    model phrased is a request whose *description* is as untrusted as the thing
    being approved.
    """
    decision = policy.decide(principal, operation)

    if decision.value == "allow":
        return Authorisation(allowed=True)
    if decision.value == "deny":
        return Authorisation(
            allowed=False,
            reason=f"policy denies {operation.value!r} for {principal.principal_id!r}",
        )

    # --- ask -------------------------------------------------------------
    request = ApprovalRequest(
        request_id=f"harness-{operation.value}-approval",
        session_id=session.header.session_id,
        tool_name=f"harness:{operation.value}",
        arguments_json="{}",
        reason=summary,
        preview={"summary": summary, "operation": operation.value},
    )
    await session.append(
        EventKind.APPROVAL_ASKED,
        {"callId": request.request_id, "tool": request.tool_name, "reason": summary},
    )
    # The same rule as the pipeline's, and for the same reason: the prompt is how
    # a human learns there is something to answer, and the answer comes back on a
    # channel that reads the log.
    await session.flush()

    try:
        outcome = await approval.request(request)
    except Exception:  # noqa: BLE001 - an answerer that breaks is not a yes
        outcome = ApprovalOutcome.UNAVAILABLE

    await session.append(
        EventKind.APPROVAL_DECIDED,
        {"callId": request.request_id, "outcome": str(outcome)},
    )

    if outcome is ApprovalOutcome.ALLOWED_ONCE:
        return Authorisation(allowed=True, asked=True)

    return Authorisation(
        allowed=False,
        asked=True,
        reason=(
            f"{operation.value!r} needs a human's approval and got {outcome.value!r}. "
            + (
                "No answerer is registered, so nothing could answer — this is a "
                "refusal, not a silent success."
                if outcome is ApprovalOutcome.UNAVAILABLE
                else ""
            )
        ).strip(),
    )
