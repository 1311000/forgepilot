"""Checking that every claim is actually backed by something.

This is the module that makes the difference between a change narrative and a
model's opinion about a change. A narrative is prose generated from untrusted
inputs — a ticket, a diff, a conversation — and prose about code is exactly the
kind of thing that reads as authoritative while being wrong.

So **nothing is verified by default**. :class:`Claim.verified` starts ``False``,
only this module sets it, and it sets it only for claims whose every
:class:`EvidenceRef` resolves against something real:

- a ``session_event`` must cite a sequence number the log actually contains;
- a ``code_ref`` must name a path that is part of *this* change;
- a ``diff_hunk`` must name a hunk that exists in *this* diff;
- a ``check`` must correspond to a check that actually ran.

A claim that fails is **not deleted**. It moves to
:attr:`ChangeNarrative.unverified` with the reason, because silently dropping it
would leave the reader unable to tell how much was removed — and because the
list of what could not be verified is itself the most useful thing here. A long
one is a signal: this change needs your attention.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from forgepilot.seams.review import (
    UNVERIFIED_NO_EVIDENCE,
    ChangeGroup,
    ChangeNarrative,
    Claim,
    EvidenceKind,
    EvidenceRef,
)

__all__ = ["EvidenceUniverse", "validate_claim", "validate_narrative"]


class EvidenceUniverse:
    """Everything a claim is allowed to point at.

    Built from reality — the session log's high-water mark, the diff's hunks, the
    set of changed paths — rather than from the narrative, so a claim cannot
    widen the set of things that count as evidence for it.
    """

    def __init__(
        self,
        *,
        high_water: int = 0,
        changed_paths: Iterable[str] = (),
        hunk_ids: Iterable[str] = (),
        check_events: Iterable[int] = (),
    ) -> None:
        self.high_water = high_water
        self.changed_paths = frozenset(changed_paths)
        self.hunk_ids = frozenset(hunk_ids)
        self.check_events = frozenset(check_events)

    @classmethod
    def from_session(cls, session: object, *, changed_paths=(), hunk_ids=(), kinds=None) -> EvidenceUniverse:
        """Build from a live session object."""
        from forgepilot.seams.session import EventKind

        events = list(getattr(session, "committed_events", ()) or ())
        checks = [e.seq for e in events if e.kind == EventKind.TASK_CHECK]
        return cls(
            high_water=max((e.seq for e in events), default=0),
            changed_paths=changed_paths,
            hunk_ids=hunk_ids,
            check_events=checks,
        )

    def resolve(self, ref: EvidenceRef) -> str:
        """Return ``''`` when the ref is real, or a reason it is not."""
        if ref.kind is EvidenceKind.SESSION_EVENT:
            if ref.session_seq is None:
                return "a session event without a sequence number"
            if ref.session_seq < 1 or ref.session_seq > self.high_water:
                return (
                    f"session #{ref.session_seq} does not exist; the log holds "
                    f"{self.high_water} events"
                )
            return ""

        if ref.kind is EvidenceKind.CODE_REF:
            if ref.code_ref is None:
                return "a code reference without a location"
            if ref.code_ref.path not in self.changed_paths:
                return (
                    f"{ref.code_ref.path!r} is not part of this change, so citing it does "
                    f"not evidence anything about it"
                )
            return ""

        if ref.kind is EvidenceKind.DIFF_HUNK:
            if not ref.hunk_id:
                return "a diff hunk reference without an id"
            if ref.hunk_id not in self.hunk_ids:
                return f"hunk {ref.hunk_id!r} is not in this diff"
            return ""

        if ref.kind is EvidenceKind.CHECK:
            if not self.check_events:
                return "no check was run, so nothing about verification can be evidenced"
            return ""

        return f"unknown evidence kind {ref.kind!r}"


def validate_claim(claim: Claim, universe: EvidenceUniverse) -> Claim:
    """Return the claim with ``verified`` set honestly, and a reason if not."""
    if not claim.text:
        return claim

    if not claim.evidence:
        return replace(claim, verified=False, unverified_reason=UNVERIFIED_NO_EVIDENCE)

    problems = [reason for ref in claim.evidence if (reason := universe.resolve(ref))]
    if problems:
        return replace(claim, verified=False, unverified_reason="; ".join(problems))

    return replace(claim, verified=True, unverified_reason="")


def validate_narrative(
    narrative: ChangeNarrative, universe: EvidenceUniverse
) -> ChangeNarrative:
    """Validate every claim, moving the failures to ``unverified``.

    Groups are validated too: a group whose rationale cannot be evidenced keeps
    its hunks — the change is still there to read — but its description is
    demoted, because an unevidenced description of a change is exactly the thing
    a reader would otherwise take on faith.
    """
    intent = validate_claim(narrative.intent, universe)
    approach = tuple(validate_claim(c, universe) for c in narrative.approach)
    risks = tuple(validate_claim(c, universe) for c in narrative.risks)

    groups: list[ChangeGroup] = []
    for group in narrative.groups:
        groups.append(replace(group, rationale=validate_claim(group.rationale, universe)))

    failing: list[Claim] = []
    for claim in (*approach, *risks, *(g.rationale for g in groups), intent):
        if claim.text and not claim.verified:
            failing.append(claim)

    # Claims the narrative already knew it could not support are kept alongside
    # the ones validation caught — they are equally things the reader should not
    # treat as established.
    unverified = tuple(failing) + tuple(narrative.unverified)

    return replace(
        narrative,
        intent=intent,
        approach=approach,
        risks=risks,
        groups=tuple(groups),
        unverified=unverified,
        evidence=_collect_evidence(narrative, groups),
    )


def _collect_evidence(
    narrative: ChangeNarrative, groups: list[ChangeGroup]
) -> tuple[EvidenceRef, ...]:
    """Every ref cited anywhere, de-duplicated, in a stable order."""
    seen: dict[tuple, EvidenceRef] = {}
    claims: list[Claim] = [narrative.intent, *narrative.approach, *narrative.risks]
    claims.extend(g.rationale for g in groups)

    for claim in claims:
        for ref in claim.evidence:
            key = (str(ref.kind), ref.session_seq, ref.hunk_id, getattr(ref.code_ref, "path", None))
            seen.setdefault(key, ref)

    for ref in narrative.blast_radius.affected_callers:
        key = (str(EvidenceKind.CODE_REF), None, None, ref.path)
        seen.setdefault(key, EvidenceRef(kind=EvidenceKind.CODE_REF, code_ref=ref, detail="call site"))

    return tuple(seen.values())


def evidence_summary(narrative: ChangeNarrative) -> Mapping[str, int]:
    """Counts by kind, for the rendered header."""
    counts: dict[str, int] = {}
    for ref in narrative.evidence:
        counts[str(ref.kind)] = counts.get(str(ref.kind), 0) + 1
    return counts
