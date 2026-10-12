"""The harness's own acts on memory: recording a resolution, forgetting, purging.

Three things happen to a memory after it is written, and they are not the same kind
of thing at all:

- **A resolution** is bookkeeping. The store compared an admission with the records
  already on that subject and decided which of them stands
  (``seams/memory.py::plan_resolution``). Nothing is authorised here — this module
  only writes down *what was decided*, so that "why does the system believe this
  instead of that?" has an answer in the log rather than in a data structure.
- **Forgetting** (``revoke``) is a decision about what the system keeps believing. It
  is reversible, it keeps the evidence, and it is a person's call: it goes through
  ``Operation.FORGET_MEMORY``.
- **Purging** deletes the content. It is the only irreversible act in the system, and
  it costs exactly what the audit trail was for: afterwards, "what did it say?" has
  no answer, only "there was a memory here, and this person removed it". It goes
  through ``Operation.PURGE_MEMORY``, and a typed confirmation at the CLI on top of
  the approval, because an approval answerer that can say yes to a prompt can also
  say yes to a prompt somebody did not read.

**The order in this module is the fail-closed part**: authorise first, act second,
log third. A store call that happens before the authorisation is a store call that
happened for a reason nobody checked — the same argument as ``tools/arm`` being the
only phase allowed to touch host state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forgepilot.runtime.authority import authorise
from forgepilot.seams.memory import PurgeTombstone, Resolution, ResolutionKind
from forgepilot.seams.refs import MemoryId
from forgepilot.seams.security import Operation, Principal
from forgepilot.seams.session import EventKind

__all__ = ["ForgetOutcome", "record_resolutions", "forget", "purge"]


@dataclass(frozen=True, slots=True)
class ForgetOutcome:
    """What came of a forget or purge: whether it happened, and why not when it did not."""

    memory_id: MemoryId
    allowed: bool
    reason: str = ""
    #: True when a human was asked (and, if ``allowed``, answered yes).
    asked: bool = False
    #: What is left of a purged memory — set only by :func:`purge` when it succeeded.
    tombstone: PurgeTombstone | None = None


async def record_resolutions(
    session: Any, resolutions: Any, *, turn: Any = None, step: Any = None
) -> tuple[str, ...]:
    """Write one ``memory/resolved`` event per decision, and return the kinds.

    One event per comparison rather than one per admission: each pair decided
    something separate, and a caller reading the log afterwards needs to see which
    record replaced which, not that "three things happened". A ``RELATED`` report —
    two memories whose subjects look alike under different keys — is written here too
    and changes nothing in the store: it exists so that a person can see the pair a
    fuzzy match found without the system acting on a fuzzy match.

    Log-only and never model-visible: the model reads memories, not the bookkeeping of
    the store they live in.
    """
    kinds: list[str] = []
    for resolution in resolutions:
        if not isinstance(resolution, Resolution):
            continue
        kinds.append(resolution.kind.value)
        await session.append(
            EventKind.MEMORY_RESOLVED,
            {
                "kind": resolution.kind.value,
                "incoming": str(resolution.incoming),
                "incumbent": str(resolution.incumbent),
                "subject": resolution.subject,
                "winner": str(resolution.winner) if resolution.winner is not None else None,
                "loser": str(resolution.loser) if resolution.loser is not None else None,
                "reason": resolution.reason,
                "conflict": resolution.kind is ResolutionKind.CONFLICT,
            },
            turn=turn,
            step=step,
        )
    return tuple(kinds)


async def forget(
    session: Any,
    *,
    memory: Any,
    memory_id: MemoryId,
    policy: Any,
    approval: Any,
    principal: Principal,
    reason: str = "",
) -> ForgetOutcome:
    """Stop returning a memory, keeping it and its evidence in the audit."""
    authorisation = await authorise(
        policy=policy,
        approval=approval,
        principal=principal,
        operation=Operation.FORGET_MEMORY,
        session=session,
        summary=(
            f"forget memory {memory_id}"
            + (f" — {reason}" if reason else "")
            + "; it stops being recalled, stays in the log, and can be undone by hand"
        ),
    )
    if not authorisation.allowed:
        return ForgetOutcome(memory_id=memory_id, allowed=False, reason=authorisation.reason)

    try:
        await memory.revoke(memory_id)
    except KeyError as exc:
        return ForgetOutcome(memory_id=memory_id, allowed=False, reason=str(exc))

    await session.append(
        EventKind.MEMORY_REVOKED,
        {
            "memoryId": str(memory_id),
            "by": str(principal.principal_id),
            "reason": reason,
            "asked": authorisation.asked,
        },
    )
    return ForgetOutcome(
        memory_id=memory_id, allowed=True, reason=authorisation.reason, asked=authorisation.asked
    )


async def purge(
    session: Any,
    *,
    memory: Any,
    memory_id: MemoryId,
    policy: Any,
    approval: Any,
    principal: Principal,
    reason: str = "",
) -> ForgetOutcome:
    """Delete a memory's content, leaving only the tombstone that says it existed.

    The tombstone is written to the **log** as well as to the store, because the log
    is the audit and the store entry is what the audit is about. If the two ever
    disagree, the log is the one that was not supposed to be editable.
    """
    authorisation = await authorise(
        policy=policy,
        approval=approval,
        principal=principal,
        operation=Operation.PURGE_MEMORY,
        session=session,
        summary=(
            f"purge memory {memory_id}"
            + (f" — {reason}" if reason else "")
            + "; its content is deleted and cannot be recovered, only the fact that it "
            "existed and who removed it will remain"
        ),
    )
    if not authorisation.allowed:
        return ForgetOutcome(memory_id=memory_id, allowed=False, reason=authorisation.reason)

    try:
        tombstone = await memory.purge(
            memory_id, approved_by=str(principal.principal_id), reason=reason
        )
    except KeyError as exc:
        return ForgetOutcome(memory_id=memory_id, allowed=False, reason=str(exc))

    await session.append(
        EventKind.MEMORY_PURGED,
        {
            "memoryId": str(tombstone.memory_id),
            "contentHash": tombstone.content_hash,
            "subject": tombstone.subject,
            "tenantId": str(tombstone.tenant_id) if tombstone.tenant_id else "",
            "purgedAt": tombstone.purged_at.isoformat(),
            "approvedBy": tombstone.approved_by,
            "reason": tombstone.reason,
        },
    )
    return ForgetOutcome(
        memory_id=memory_id,
        allowed=True,
        reason=authorisation.reason,
        asked=authorisation.asked,
        tombstone=tombstone,
    )
