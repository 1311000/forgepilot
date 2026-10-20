"""Long-term-memory seam — service name ``memory``.

The design draws a hard line: **a session records what happened; memory holds
what is worth reusing.** Three rules follow, and all three are enforced
structurally rather than left to discipline:

- Not all history becomes memory.
- A model's unverified assertion never becomes fact. ``write`` rejects
  :attr:`ProvenanceKind.MODEL_ASSERTED` outright; such candidates can only go to
  :meth:`MemoryStore.quarantine`, and ``recall`` never returns them.
- **A trust level without evidence behind it is not a trust level.** ``write``
  refuses a record that cites nothing, because a label everything downstream
  believes is worse than an unlabelled record — the same argument as refusing an
  unsigned ``ExecutionConstraints``.

⚠️ **Only ``REPO`` scope has a defined audience.** The plan assumed a memory
scope "mirrors the repository scope"; that holds for ``REPO`` and collapses for
``TEAM``/``PROJECT``/``ORG``, because a team is not a set of repositories. Rather
than guess, :func:`audience_repo` refuses them. **An undefined audience is not an
open one** — and it is not an empty one either, which is why recall raises rather
than returning nothing.

**M11b adds the rest of the record, and each field has a reader.** A memory now
carries a :class:`MemoryType` (what kind of thing it is), a
:class:`Confidence` (how strongly the *harness* believes it, computed from the
evidence — never self-reported), a :class:`MemoryStatus` (where it is in its life,
with illegal transitions refused), a normalised ``subject`` (the key conflict
detection will use), and a ``tenant_id``. Two of those deserve their reason here:

- **Confidence is computed, and the model's own number is kept only as
  ``model_confidence_hint``.** A field the model fills in and the system trusts is
  the repeated defect shape with a new name.
- **A candidate with no tenant cannot be admitted.** The alternative is a record
  visible to every tenant, which is not a weaker check but an absent one, and it
  would look exactly like a record nobody got round to classifying.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from forgepilot.errors import ForgePilotError
from forgepilot.seams.refs import MemoryId, Mode, RepoRef, SessionId, TenantId
from forgepilot.seams.security import ExecutionConstraints

__all__ = [
    "SERVICE_NAME",
    "UnattributableScope",
    "UnattributableTenant",
    "IllegalTransition",
    "audience_repo",
    "normalize_subject",
    "content_hash_of",
    "confidence_for",
    "assert_transition",
    "MemoryScope",
    "MemoryType",
    "MemoryStatus",
    "Confidence",
    "ProvenanceKind",
    "EventRef",
    "Provenance",
    "MemoryCandidate",
    "MemoryRecord",
    "RecallQuery",
    "MemoryStore",
    "UnprovenMemoryWrite",
    "RecallVerdict",
    "require_tenant",
    "require_audience",
    "require_recallable_provenance",
    "require_provisional_provenance",
    "new_recallable_record",
    "new_provisional_record",
    "new_quarantined_record",
    "promote_proposal",
    "confirm_record",
    "revoke_record",
    "expire_record",
    "count_recall",
    "may_recall",
    "matches_text",
    "text_tokens",
    "recall_verdict",
    "TRUST_ORDER",
    "trust_rank",
    "strength_of",
    "expiry_for",
    "ResolutionKind",
    "Resolution",
    "PurgeTombstone",
    "PruneReport",
    "PRUNABLE_STATUSES",
    "prunable",
    "conflict_key",
    "siblings_of",
    "subjects_are_related",
    "resolve_against",
    "related_to",
    "plan_resolution",
    "supersede_record",
    "mark_conflicted",
    "note_replacement",
    "RECALLABLE_PROVENANCE",
    "CONFIDENCE_FOR_PROVENANCE",
    "LEGAL_TRANSITIONS",
    "MEMORY_TYPE_MODES",
    "MEMORY_TYPE_EXPIRY_DAYS",
    "SUBJECT_SIMILARITY_THRESHOLD",
    "INDEPENDENT_EVIDENCE_FOR_RAISE",
    "REVOCABLE_STATUSES",
    "RECALLABLE_STATUSES",
    "COMPETING_STATUSES",
]

SERVICE_NAME = "memory"


class UnattributableScope(ForgePilotError):
    """A candidate's ``scope_ref`` does not name the kind of thing its scope says.

    Raised rather than defaulted, because the alternative — returning "no repo"
    for a malformed ref — is indistinguishable from "this scope has no repo
    dimension", and that is precisely how a record escapes the permission check.
    """


class MemoryScope(StrEnum):
    """Who a memory is for.

    ⚠️ Only :attr:`REPO` has a defined audience model today: ``scope_ref`` is a
    repository ``host/namespace/name``, and recall checks it against the caller's
    constraints. ``PROJECT``/``TEAM``/``ORG`` name an audience the design has not
    defined — a team is not a set of repositories, so "mirror the repo scope" is
    not an answer — and recall therefore **refuses** them rather than guessing.
    """

    REPO = "repo"
    PROJECT = "project"
    TEAM = "team"
    ORG = "org"


def audience_repo(scope: MemoryScope | str, scope_ref: str) -> RepoRef:
    """The repository whose audience a scope names.

    The one place that knows what a ``scope_ref`` means, so no provider has to
    re-derive it — and so a provider that *forgets* to check cannot look like one
    that checked and found nothing.

    Raises for a scope with no defined audience model, and for a ``REPO`` ref
    that is not ``host/namespace/name``. **The raise is not "no restriction
    applies"** — it is the opposite. An audience nobody has defined is an
    audience nobody can authorise, and returning "everything" or "nothing" for it
    would both be answers the design has not earned.
    """
    resolved = MemoryScope(scope)
    if resolved is not MemoryScope.REPO:
        raise UnattributableScope(
            f"the {resolved.value!r} memory scope has no defined audience model, so "
            f"nobody can say who may read it. The plan assumed a memory scope mirrors "
            f"the repository scope, and that breaks the moment the scope is not a "
            f"repository — a team is not a set of repositories. Only 'repo' is "
            f"defined; the rest refuse until the question is answered."
        )
    parts = scope_ref.split("/")
    if len(parts) != 3 or not all(parts):
        raise UnattributableScope(
            f"a REPO-scoped memory must name its repository as 'host/namespace/name'; "
            f"got {scope_ref!r}. A ref that cannot be attributed cannot be checked "
            f"against anyone's permission, so it is refused rather than defaulted."
        )
    return RepoRef(host=parts[0], namespace=parts[1], name=parts[2])


def require_audience(candidate: MemoryCandidate) -> None:
    """Refuse a candidate whose scope names no audience, **at the write**.

    :func:`audience_repo` already raises at recall, and that alone was not enough:
    a ``TEAM``-scoped candidate was accepted, stored, and then unreachable by any
    recall — a record nobody can read, created by a path that answered "yes". The
    failure has to happen where the caller can still do something about it, which
    is the write.

    Quarantine is deliberately exempt (see :func:`new_quarantined_record`): nothing
    recalls a quarantined record, so it has no audience to get wrong.
    """
    audience_repo(candidate.scope, candidate.scope_ref)


class UnattributableTenant(ForgePilotError):
    """A candidate names no tenant, so no one can say who may read it.

    The same refusal as :class:`UnattributableScope`, one level out. A record
    without a tenant is not "visible to the tenant that asks" — it is visible to
    *every* tenant, because the filter that would exclude it has nothing to
    compare against. Defaulting it (to the caller's, to ``"default"``) would make
    the leak a silent one, so the write is refused instead.
    """


class MemoryType(StrEnum):
    """What kind of thing a memory is. Chosen by the model, stored by the seam.

    The type is not decoration: it decides **where the memory is allowed to be
    recalled** (:data:`MEMORY_TYPE_MODES`), which is how a task summary stops
    being recited back during a chat, and it is part of the identity a conflict is
    detected on.
    """

    COMMAND = "command"
    PREFERENCE = "preference"
    FACT = "fact"
    DECISION = "decision"
    PLAN = "plan"
    TASK_SUMMARY = "task_summary"
    PITFALL = "pitfall"
    CONVENTION = "convention"


#: Where each type belongs. The three user-facing modes are one backend, so this
#: is a *render filter*, not a code branch: a chat recall never asks for
#: ``TASK_SUMMARY``, and a review never asks for a command.
MEMORY_TYPE_MODES: Mapping[MemoryType, frozenset[Mode]] = {
    MemoryType.COMMAND: frozenset({Mode.CHAT, Mode.TASK}),
    MemoryType.PREFERENCE: frozenset({Mode.CHAT, Mode.TASK}),
    MemoryType.FACT: frozenset({Mode.CHAT, Mode.TASK}),
    MemoryType.DECISION: frozenset({Mode.CHAT, Mode.TASK, Mode.REVIEW}),
    MemoryType.PLAN: frozenset({Mode.CHAT, Mode.TASK}),
    MemoryType.TASK_SUMMARY: frozenset({Mode.TASK}),
    MemoryType.PITFALL: frozenset({Mode.CHAT, Mode.TASK}),
    MemoryType.CONVENTION: frozenset({Mode.CHAT, Mode.TASK, Mode.REVIEW}),
}


class Confidence(StrEnum):
    """How strongly the *harness* believes a memory, having read the evidence.

    Ordered, and deliberately coarse. The value is computed by
    :func:`confidence_for` from the provenance kind and the number of independent
    events behind it; a model's own number is stored beside it as
    ``model_confidence_hint`` and never consulted for this.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    def rank(self) -> int:
        return _CONFIDENCE_ORDER.index(self)

    def raised(self) -> Confidence:
        """One tier up, saturated. Used when independent evidence agrees."""
        return _CONFIDENCE_ORDER[min(self.rank() + 1, len(_CONFIDENCE_ORDER) - 1)]

    def lowered(self) -> Confidence:
        """One tier down, saturated. Used when evidence contradicts."""
        return _CONFIDENCE_ORDER[max(self.rank() - 1, 0)]


_CONFIDENCE_ORDER: tuple[Confidence, ...] = (
    Confidence.LOW,
    Confidence.MEDIUM,
    Confidence.HIGH,
)


class MemoryStatus(StrEnum):
    """Where a memory is in its life.

    ``PROPOSED`` is the state for something nothing has endorsed yet; it is
    recallable *as a proposal* and must be rendered as one. ``CONFLICTED`` is not
    a failure state to be resolved by picking a winner: two records that disagree
    at the same trust level both stay, and the disagreement is the information.
    """

    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    EXPIRED = "expired"
    CONFLICTED = "conflicted"


#: Statuses ``prune`` may delete rows in. **Every one of them is terminal, and that
#: is the whole safety argument**: a pruned row cannot be one recall would have
#: served, because no status here is in :data:`RECALLABLE_STATUSES`. The store is a
#: cache of current belief; the *audit* is the session log, which keeps
#: ``memory/write`` and ``memory/resolved`` long after the row is gone.
PRUNABLE_STATUSES: frozenset[MemoryStatus] = frozenset(
    {MemoryStatus.SUPERSEDED, MemoryStatus.REVOKED, MemoryStatus.EXPIRED}
)


def prunable(record: MemoryRecord, *, cutoff: datetime) -> bool:
    """Whether this row is history old enough to delete — the rule both stores share.

    Age is taken from ``updated_at or created_at``: a record superseded yesterday is
    yesterday's news even if it was admitted a year ago, and using ``created_at``
    would delete a row that only just became history.
    """
    if record.status not in PRUNABLE_STATUSES:
        return False
    moment = record.updated_at or record.created_at
    return moment is not None and moment <= cutoff


#: The statuses recall may return. A quarantined record is not here by
#: construction, ``REVOKED``/``EXPIRED`` are terminal, and ``SUPERSEDED`` is
#: history — kept because the log keeps it, never served as current.
RECALLABLE_STATUSES: frozenset[MemoryStatus] = frozenset(
    {MemoryStatus.ACTIVE, MemoryStatus.PROPOSED, MemoryStatus.CONFLICTED}
)

#: Statuses ``revoke`` accepts. Revoking a terminal record is a no-op rather than
#: an error: the caller's intent ("this must not be recalled") is already met.
REVOCABLE_STATUSES: frozenset[MemoryStatus] = frozenset(
    {MemoryStatus.PROPOSED, MemoryStatus.ACTIVE, MemoryStatus.CONFLICTED}
)

#: Every legal move. Absent means illegal, and illegal means an exception — never
#: a silent assignment, because the status is what every reader filters on.
LEGAL_TRANSITIONS: Mapping[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.PROPOSED: frozenset(
        {MemoryStatus.ACTIVE, MemoryStatus.REVOKED, MemoryStatus.EXPIRED}
    ),
    MemoryStatus.ACTIVE: frozenset(
        {
            MemoryStatus.SUPERSEDED,
            MemoryStatus.REVOKED,
            MemoryStatus.EXPIRED,
            MemoryStatus.CONFLICTED,
        }
    ),
    MemoryStatus.CONFLICTED: frozenset(
        {
            MemoryStatus.ACTIVE,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.REVOKED,
            MemoryStatus.EXPIRED,
        }
    ),
    MemoryStatus.SUPERSEDED: frozenset(),
    MemoryStatus.REVOKED: frozenset(),
    MemoryStatus.EXPIRED: frozenset(),
}


class IllegalTransition(ForgePilotError):
    """A status change the state machine does not have an edge for."""


def assert_transition(current: MemoryStatus, target: MemoryStatus) -> None:
    """Refuse an illegal status change, naming both ends.

    A no-op when ``current is target``: setting a field to the value it already
    has is not a transition, and refusing it would make idempotent replay fail.
    """
    if current is target:
        return
    if target not in LEGAL_TRANSITIONS[current]:
        allowed = sorted(status.value for status in LEGAL_TRANSITIONS[current])
        raise IllegalTransition(
            f"cannot move a memory from {current.value!r} to {target.value!r}; "
            f"the legal moves from {current.value!r} are {allowed}. Statuses are "
            f"append-only in the same sense the log is: a record whose conclusion "
            f"changed is superseded by a new record, not rewritten in place."
        )


def normalize_subject(raw: str, *, limit: int = 120) -> str:
    """The key a conflict is detected on: lower, unaccented, punctuation-stripped.

    The model proposes a subject and this normalises it, because two memories
    about the same thing must collide *even when a model words them differently* —
    and because the model does not get to control the key that decides whether its
    own record is a duplicate.
    """
    folded = unicodedata.normalize("NFKC", raw).casefold()
    words = re.findall(r"[^\W_]+", folded, flags=re.UNICODE)
    return " ".join(words)[:limit]


@dataclass(frozen=True, slots=True)
class EventRef:
    """A position in a durable session log: which session, which sequence.

    Evidence used to be a bare string, which made "verify the citation" — the
    thing that separates a memory from a rumour — impossible to even express. A
    typed ref can be checked against a store; ``"seq:12"`` cannot.
    """

    session_id: SessionId
    seq: int

    def __str__(self) -> str:
        return f"{self.session_id}#{self.seq}"


class ProvenanceKind(StrEnum):
    """How a candidate came to be believed.

    Ordered by trust. Only the first three are recallable.
    """

    HUMAN_VERIFIED = "human_verified"
    TEST_VERIFIED = "test_verified"
    TOOL_OBSERVED = "tool_observed"
    #: The model said so. Never recallable as fact.
    MODEL_ASSERTED = "model_asserted"


RECALLABLE_PROVENANCE = frozenset(
    {
        ProvenanceKind.HUMAN_VERIFIED,
        ProvenanceKind.TEST_VERIFIED,
        ProvenanceKind.TOOL_OBSERVED,
    }
)

#: The mechanical form of "the system's own output and the user's instruction rank
#: high; the model's inference ranks low". Read off the provenance kind, which the
#: *harness* assigns from the event that evidences the memory — never off the
#: model's own number.
CONFIDENCE_FOR_PROVENANCE: Mapping[ProvenanceKind, Confidence] = {
    ProvenanceKind.HUMAN_VERIFIED: Confidence.HIGH,
    ProvenanceKind.TEST_VERIFIED: Confidence.HIGH,
    ProvenanceKind.TOOL_OBSERVED: Confidence.MEDIUM,
    ProvenanceKind.MODEL_ASSERTED: Confidence.LOW,
}

#: Two independent events saying the same thing is one tier of belief more than one
#: event saying it. The threshold is a *count of distinct refs*, not a weight: a
#: mechanism nobody can audit is one nobody can argue with.
INDEPENDENT_EVIDENCE_FOR_RAISE = 2


def confidence_for(kind: ProvenanceKind, evidence: Sequence[EventRef]) -> Confidence:
    """The belief level the harness computes, from the kind and the evidence.

    Raises nothing and clamps at ``HIGH``: more agreement cannot make a memory
    more than believed, which is what keeps "high confidence" from becoming a
    synonym for "we have a lot of copies of the same rumour".
    """
    base = CONFIDENCE_FOR_PROVENANCE[kind]
    if len(set(evidence)) >= INDEPENDENT_EVIDENCE_FOR_RAISE:
        return base.raised()
    return base


@dataclass(frozen=True, slots=True)
class Provenance:
    kind: ProvenanceKind
    session_id: SessionId | None = None
    repo: RepoRef | None = None
    #: Pointers to the evidence: positions in a durable session log. A record with
    #: no evidence is an assertion, not a memory — and now that the refs are typed,
    #: "does this citation exist" is a question a store can actually answer.
    evidence_refs: tuple[EventRef, ...] = ()

    @property
    def is_recallable(self) -> bool:
        return self.kind in RECALLABLE_PROVENANCE


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """A prospective memory, before it has been admitted.

    Everything the *model* proposes lives here: the sentence, its type, the subject
    it thinks it is about, and its own confidence number. Everything the *harness*
    decides — how much it is believed, whether it is active — lives on
    :class:`MemoryRecord`, which is why a candidate cannot claim a belief level.
    """

    text: str
    scope: MemoryScope
    scope_ref: str
    type: MemoryType = MemoryType.FACT
    #: The model's proposed key. The store normalises it (:func:`normalize_subject`)
    #: before it decides anything with it.
    subject: str = ""
    #: D5. ``None`` is refused at the write path, not defaulted: a record nobody
    #: can attribute to a tenant is readable by all of them.
    tenant_id: TenantId | None = None
    #: The model's own number, 0..1 or None. Stored beside the computed confidence,
    #: never used in place of it.
    model_confidence_hint: float | None = None
    #: When this claim stops being current, if it does. Set by the **distiller**
    #: from its per-type policy (:class:`MemoryType.PLAN` and
    #: :class:`MemoryType.TASK_SUMMARY` go stale; a convention does not) — the
    #: model's output has no field for it, so a memory cannot be made immortal by
    #: asking nicely. Observed at recall time rather than by a sweeper.
    expires_at: datetime | None = None
    #: Placeholder kinds this text already carries, if it was distilled from an
    #: event the redaction gate had rewritten. A memory built from half a sentence
    #: must say so, or the prompt states it as a whole fact.
    redaction_marks: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def normalized_subject(self) -> str:
        return normalize_subject(self.subject or self.text)


def content_hash_of(candidate: MemoryCandidate) -> str:
    """Identity for de-duplication: the same claim, from any session, once.

    Over the *normalised* subject and the folded text, so re-distilling the same
    conversation — or distilling it twice because a watermark moved — cannot write
    a second copy. It deliberately excludes ``created_at`` and the session id:
    including them would defeat the entire purpose of a de-duplication key.

    The text is folded the same way a subject is, because two statements that
    differ only in case, spacing or punctuation are the same claim. The **subject
    is not the identity**, though: two different facts about one subject are two
    memories, and merging them would lose one — the failure a de-duplication key is
    easiest to cause.
    """
    payload = "\x1f".join(
        (
            candidate.tenant_id or "",
            candidate.scope.value,
            candidate.scope_ref,
            candidate.type.value,
            candidate.normalized_subject,
            normalize_subject(candidate.text, limit=_HASH_TEXT_LIMIT),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


#: Long enough that a truncation collision needs two claims identical in their
#: first two thousand normalised characters. Short enough to bound the key.
_HASH_TEXT_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """An admitted memory: what was proposed, plus what the harness decided.

    Immutable, like the log and for the same reason. "Updating" a memory writes a
    new record and moves the old one to ``SUPERSEDED``; the old version is never
    overwritten, so "auditable, reconstructible" is a property of the storage
    rather than a promise in a docstring.
    """

    id: MemoryId
    candidate: MemoryCandidate
    provenance: Provenance
    #: Computed by :func:`confidence_for` at admission. Only :meth:`MemoryStore.confirm`
    #: and a contradiction may move it, and each move emits an event.
    confidence: Confidence = Confidence.LOW
    status: MemoryStatus = MemoryStatus.ACTIVE
    #: Copied off the candidate at admission, where it is required.
    tenant_id: TenantId | None = None
    #: The *normalised* subject, computed by the store — never the raw proposal.
    subject: str = ""
    #: :func:`content_hash_of` of the candidate. The de-duplication key.
    content_hash: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    #: Optional, by type (see the plan's §六). ``EXPIRED`` is reached when a recall
    #: observes the deadline rather than by a timer nobody runs.
    expires_at: datetime | None = None
    supersedes: MemoryId | None = None
    superseded_by: MemoryId | None = None
    recall_count: int = 0
    last_recalled_at: datetime | None = None

    @property
    def type(self) -> MemoryType:
        return self.candidate.type

    @property
    def text(self) -> str:
        return self.candidate.text

    @property
    def is_verified(self) -> bool:
        """Whether this may be stated as fact rather than as a proposal.

        False for anything the harness has not endorsed, which is what lets a
        renderer put proposals in their own block instead of quietly asserting
        them.
        """
        return self.status is MemoryStatus.ACTIVE and self.provenance.kind in {
            ProvenanceKind.HUMAN_VERIFIED,
            ProvenanceKind.TEST_VERIFIED,
        }


@dataclass(frozen=True, slots=True)
class RecallQuery:
    text: str
    scope: MemoryScope
    scope_ref: str
    limit: int = 10


# -- the admission and recall rules, in one place -----------------------------
#
# There are two stores now — process memory and a SQLite file — and a second
# implementation is exactly where "a rule that is usually respected" turns into two
# different answers to "what may be believed". So the decisions live here, as
# functions over the value objects above, and each store keeps only its storage: a
# dict or a row, plus the lines that call these.
#
# Nothing here reads a clock: ``now`` is a parameter, so a store's expiry behaviour
# is something a test can pin rather than something that depends on when it ran.


class UnprovenMemoryWrite(ForgePilotError):
    """A caller tried to admit something at a trust level its evidence cannot carry."""


class RecallVerdict(StrEnum):
    """What one record means to one query."""

    #: It answers the query: count it and return it.
    HIT = "hit"
    #: Not this query's business, or not recallable at all.
    SKIP = "skip"
    #: The deadline has passed — mark it ``EXPIRED`` and do not return it.
    EXPIRED = "expired"


def require_tenant(candidate: MemoryCandidate) -> TenantId:
    """The tenant a candidate names, or a refusal to guess one."""
    if candidate.tenant_id is None:
        raise UnattributableTenant(
            "refusing to admit a memory whose candidate names no tenant. The "
            "recall filter compares the caller's tenant with the record's, so a "
            "record without one is not merely under-classified: it passes every "
            "comparison. Set MemoryCandidate.tenant_id from the session that "
            "evidenced the memory."
        )
    return candidate.tenant_id


def require_recallable_provenance(provenance: Provenance) -> None:
    """Refuse a write whose evidence cannot carry the trust it claims."""
    if not provenance.is_recallable:
        allowed = sorted(kind.value for kind in RECALLABLE_PROVENANCE)
        raise UnprovenMemoryWrite(
            f"refusing to admit a {provenance.kind.value!r} candidate as recallable "
            f"memory. Only {allowed} may be written; use admit_provisional() or "
            f"quarantine() for anything else. Promoting a model's guess to fact is "
            f"the failure this check exists to prevent."
        )
    if not provenance.evidence_refs:
        # A trust level is a claim *about evidence*. With none cited, the level is
        # decoration — and decoration is worse than an untrusted record, because
        # everything downstream believes the label. The same argument as refusing an
        # unsigned ExecutionConstraints: omitting the evidence must not be the cheap
        # way past the evidence requirement.
        raise UnprovenMemoryWrite(
            f"refusing to admit a {provenance.kind.value!r} candidate that cites no "
            f"evidence. A record with no evidence is an assertion, not a memory — "
            f"cite the session event that supports it, or use admit_provisional() "
            f"to keep it without believing it."
        )


def require_provisional_provenance(provenance: Provenance) -> None:
    """Refuse to file an endorsed claim as a mere proposal."""
    if provenance.is_recallable:
        raise UnprovenMemoryWrite(
            f"refusing to file a {provenance.kind.value!r} candidate as a mere "
            f"proposal. That kind claims an endorsement, whether or not it cites "
            f"one — write() is the path that decides what an endorsement is worth, "
            f"and filing it here would lose it while looking like it was kept."
        )


def new_recallable_record(
    memory_id: MemoryId,
    candidate: MemoryCandidate,
    provenance: Provenance,
    *,
    now: datetime,
) -> MemoryRecord:
    """A record admitted by ``write()``."""
    return MemoryRecord(
        id=memory_id,
        candidate=candidate,
        provenance=provenance,
        confidence=confidence_for(provenance.kind, provenance.evidence_refs),
        status=MemoryStatus.ACTIVE,
        tenant_id=candidate.tenant_id,
        subject=candidate.normalized_subject,
        content_hash=content_hash_of(candidate),
        created_at=now,
        updated_at=now,
        expires_at=expiry_for(candidate, now=now),
    )


def new_provisional_record(
    memory_id: MemoryId,
    candidate: MemoryCandidate,
    provenance: Provenance,
    *,
    now: datetime,
) -> MemoryRecord:
    """A record filed by ``admit_provisional()``.

    ``PROPOSED`` and ``LOW`` are forced rather than defaulted: there is no argument
    to anything on the write path that raises them. ``expires_at`` is carried like
    any other admission, because a proposal is in ``RECALLABLE_STATUSES`` and its
    deadline is observed by a recall.
    """
    return MemoryRecord(
        id=memory_id,
        candidate=candidate,
        provenance=provenance,
        confidence=Confidence.LOW,
        status=MemoryStatus.PROPOSED,
        tenant_id=candidate.tenant_id,
        subject=candidate.normalized_subject,
        content_hash=content_hash_of(candidate),
        created_at=now,
        updated_at=now,
        expires_at=expiry_for(candidate, now=now),
    )


def new_quarantined_record(
    memory_id: MemoryId,
    candidate: MemoryCandidate,
    provenance: Provenance,
    *,
    now: datetime,
) -> MemoryRecord:
    """A record filed by ``quarantine()``.

    No tenant is required, deliberately: a quarantined record is never returned by
    recall, so there is no audience for it to leak to. Demanding one would refuse
    the single path whose whole purpose is to keep something without believing it.
    """
    return MemoryRecord(
        id=memory_id,
        candidate=candidate,
        provenance=provenance,
        confidence=confidence_for(provenance.kind, provenance.evidence_refs),
        status=MemoryStatus.PROPOSED,
        tenant_id=candidate.tenant_id,
        subject=candidate.normalized_subject,
        content_hash=content_hash_of(candidate),
        created_at=now,
    )


def promote_proposal(
    record: MemoryRecord, provenance: Provenance, *, now: datetime
) -> MemoryRecord:
    """A proposal that the same claim has now arrived with an endorsement for.

    The claim is already stored, so a second record would be a duplicate — but a
    *better* provenance must not be silently dropped either, so the proposal is
    promoted in place. Either way the caller is answered with the id of the record
    that stands: it is the answer to "where is this memory now", not "what did I
    just create".
    """
    promoted = replace(
        record,
        status=MemoryStatus.ACTIVE,
        confidence=confidence_for(provenance.kind, provenance.evidence_refs),
        provenance=provenance,
        updated_at=now,
    )
    assert_transition(record.status, promoted.status)
    return promoted


def confirm_record(record: MemoryRecord, evidence: EventRef, *, now: datetime) -> MemoryRecord:
    """A person answered, which is the strongest evidence there is.

    It is still *evidence*: the ref is kept alongside whatever was cited before, so
    the record's history does not shrink when it is upgraded.
    """
    confirmed = replace(
        record,
        status=MemoryStatus.ACTIVE,
        confidence=Confidence.HIGH,
        provenance=Provenance(
            kind=ProvenanceKind.HUMAN_VERIFIED,
            session_id=evidence.session_id,
            repo=record.provenance.repo,
            evidence_refs=(*record.provenance.evidence_refs, evidence),
        ),
        updated_at=now,
    )
    assert_transition(record.status, confirmed.status)
    return confirmed


def revoke_record(record: MemoryRecord, *, now: datetime) -> MemoryRecord:
    """Forgetting is a status, not a deletion: the audit trail survives it."""
    revoked = replace(record, status=MemoryStatus.REVOKED, updated_at=now)
    assert_transition(record.status, revoked.status)
    return revoked


def expire_record(record: MemoryRecord, *, now: datetime) -> MemoryRecord:
    expired = replace(record, status=MemoryStatus.EXPIRED, updated_at=now)
    assert_transition(record.status, expired.status)
    return expired


def count_recall(record: MemoryRecord, *, now: datetime) -> MemoryRecord:
    """Counting a recall is the only way to answer "was this ever used" later.

    Only the counter and the timestamp move.
    """
    return replace(record, recall_count=record.recall_count + 1, last_recalled_at=now)


# -- conflicts, supersession, expiry -------------------------------------------
#
# Which of two memories about the same subject should be believed is a *decision*,
# and it lives here with the others rather than in each store: a deployment that
# swaps the store must not get a second, slightly different answer to "may this be
# believed". A store's job is to find the records that share a key and to apply the
# answer; the answer itself is :func:`resolve_against`.

#: Most trusted first. The order *is* the disagreement policy: a record's kind
#: decides, and recency only ever decides between equals. Removing the order — or
#: comparing timestamps alone — is the counterfactual this phase is measured
#: against, because "a conclusion a model guessed replaced one a test proved" is
#: the failure the whole trust model exists to prevent.
TRUST_ORDER: tuple[ProvenanceKind, ...] = (
    ProvenanceKind.HUMAN_VERIFIED,
    ProvenanceKind.TEST_VERIFIED,
    ProvenanceKind.TOOL_OBSERVED,
    ProvenanceKind.MODEL_ASSERTED,
)


def trust_rank(kind: ProvenanceKind) -> int:
    """Higher is more trusted. Total, so no pair of kinds is unranked."""
    return len(TRUST_ORDER) - 1 - TRUST_ORDER.index(kind)


#: ``created_at`` is always set by the stores; this is the fallback that keeps the
#: comparison total if one is ever missing, rather than raising inside a recall.
_EPOCH = datetime.min.replace(tzinfo=UTC)


def strength_of(record: MemoryRecord) -> tuple[int, datetime]:
    """How substantial a record is: trust first, then recency.

    The same order :func:`resolve_against` uses. A store that picked the "strongest"
    sibling by some other key would resolve one pair one way and then name a
    different sibling as the one that supersedes it — the sort of divergence that
    only shows up in the audit trail, months later.
    """
    return (trust_rank(record.provenance.kind), record.created_at or _EPOCH)


#: How long a kind of memory stays current, in days — absent means "does not expire
#: on its own". A plan and a task summary describe a state of the world that the next
#: task invalidates; a convention or a preference is not dated that way. The deadline
#: is *observed* when a record is recalled (see :func:`recall_verdict`) rather than
#: swept by a timer, because a store has no clock to sweep with and a background
#: timer would be a second thing that has to be running for the guarantee to hold.
MEMORY_TYPE_EXPIRY_DAYS: Mapping[MemoryType, int] = {
    MemoryType.PLAN: 90,
    MemoryType.TASK_SUMMARY: 90,
}


def expiry_for(candidate: MemoryCandidate, *, now: datetime) -> datetime | None:
    """When this candidate stops being current — an explicit deadline wins.

    The policy is applied at admission rather than left to the caller, because a
    per-type default that no code reads is the repeated defect shape: a field that
    looks enforced and is not.
    """
    if candidate.expires_at is not None:
        return candidate.expires_at
    days = MEMORY_TYPE_EXPIRY_DAYS.get(candidate.type, 0)
    return now + timedelta(days=days) if days else None


class ResolutionKind(StrEnum):
    """What one comparison decided."""

    #: The newcomer is the one that stands; the incumbent is history.
    SUPERSEDES = "supersedes"
    #: The incumbent stands, and the newcomer goes to history.
    SUPERSEDED = "superseded"
    #: Indistinguishable by every rule there is, so nothing is chosen.
    CONFLICT = "conflict"
    #: Same subject, different key. Reported to a person; no status changes.
    RELATED = "related"


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of comparing one incoming record with one incumbent."""

    kind: ResolutionKind
    incoming: MemoryId
    incumbent: MemoryId
    subject: str
    #: The record that stands, or ``None`` when the comparison could not choose.
    winner: MemoryId | None
    reason: str

    @property
    def loser(self) -> MemoryId | None:
        if self.winner is None:
            return None
        return self.incumbent if self.winner == self.incoming else self.incoming


def conflict_key(record: MemoryRecord) -> tuple[str, str, str, str, str]:
    """What "the same subject" means, mechanically.

    Tenant, scope, scope_ref and type have to match: two repositories' build commands
    are not two opinions about one thing, and a fact and a preference about the same
    two words are not in conflict either. The subject compared here is the
    normalised one, so the variation the plan's fallback was aimed at (case,
    punctuation, accents, spacing) has already been folded away before anything
    reads these keys.
    """
    return (
        str(record.tenant_id or ""),
        str(record.candidate.scope),
        record.candidate.scope_ref,
        str(record.type),
        record.subject,
    )


def siblings_of(records: Sequence[MemoryRecord], record: MemoryRecord) -> tuple[MemoryRecord, ...]:
    """The records that share this one's key — the only ones worth comparing."""
    key = conflict_key(record)
    return tuple(other for other in records if other.id != record.id and conflict_key(other) == key)


#: The statuses that take part in a resolution at all.
#:
#: A ``PROPOSED`` record is a candidate the harness has not checked, not a belief it
#: holds: it cannot outrank a claim, and it is **not** made history by one either.
#: Superseding it would silently retire a candidate a person might still want to
#: confirm, and the recall side already shows proposals in their own block, so it
#: cannot be mistaken for a competing belief. ``EXPIRED``, ``REVOKED`` and
#: ``SUPERSEDED`` are over — a resolution does not argue with the past.
COMPETING_STATUSES: frozenset[MemoryStatus] = frozenset(
    {MemoryStatus.ACTIVE, MemoryStatus.CONFLICTED}
)


#: Jaccard overlap at which two *different* subjects are worth mentioning to a
#: person. Deliberately blunt: this number decides what gets *reported*, never what
#: gets demoted.
SUBJECT_SIMILARITY_THRESHOLD = 0.6


def subjects_are_related(
    left: str, right: str, *, threshold: float = SUBJECT_SIMILARITY_THRESHOLD
) -> bool:
    """Whether two different keys look like the same subject worded differently.

    The plan's 词面相似度兜底, with the limit the plan does not state: a fuzzy match is
    good enough to *tell a person* that two memories may be about one thing, and not
    good enough to demote a memory the harness verified. So this feeds
    :attr:`ResolutionKind.RELATED` — a report — and never a status change.
    """
    left_words = set(text_tokens(left))
    right_words = set(text_tokens(right))
    if not left_words or not right_words:
        return False
    return len(left_words & right_words) / len(left_words | right_words) >= threshold


def resolve_against(
    incumbent: MemoryRecord, incoming: MemoryRecord, *, now: datetime
) -> Resolution:
    """Which of two same-subject records stands — never simply the newer one.

    1. A strictly more trusted *kind* wins. This is the rule the phase exists for.
    2. Between equal kinds, the more recent record wins — a later observation about
       the same subject is better information than an earlier one.
    3. Equal on both ⇒ :attr:`ResolutionKind.CONFLICT`, with **both** records kept
       and marked ``CONFLICTED``. Nothing here is a basis for choosing, and a silent
       winner would be the system asserting something it cannot support.
       ``CONFLICTED`` is still recallable, so a conflict reaches the model — in the
       unconfirmed block, labelled as not checked.

    ``now`` is only a fallback for a record with no ``created_at``; every store sets
    one. Records are compared by their own timestamps rather than by the cited
    events' times: an :class:`EventRef` carries a position in a log, not a time, and
    inventing an order for them would be worse than comparing admissions.
    """
    incoming_kind = incoming.provenance.kind
    incumbent_kind = incumbent.provenance.kind
    subject = incoming.subject or incumbent.subject
    incoming_rank = trust_rank(incoming_kind)
    incumbent_rank = trust_rank(incumbent_kind)
    if incoming_rank != incumbent_rank:
        incoming_wins = incoming_rank > incumbent_rank
        winner, loser = (incoming, incumbent) if incoming_wins else (incumbent, incoming)
        return Resolution(
            kind=ResolutionKind.SUPERSEDES if incoming_wins else ResolutionKind.SUPERSEDED,
            incoming=incoming.id,
            incumbent=incumbent.id,
            subject=subject,
            winner=winner.id,
            reason=(
                f"{winner.provenance.kind} outranks {loser.provenance.kind}: "
                f"{winner.id} stands and {loser.id} is kept as history"
            ),
        )
    incoming_at = incoming.created_at or now
    incumbent_at = incumbent.created_at or now
    if incoming_at != incumbent_at:
        incoming_wins = incoming_at > incumbent_at
        winner, loser = (incoming, incumbent) if incoming_wins else (incumbent, incoming)
        return Resolution(
            kind=ResolutionKind.SUPERSEDES if incoming_wins else ResolutionKind.SUPERSEDED,
            incoming=incoming.id,
            incumbent=incumbent.id,
            subject=subject,
            winner=winner.id,
            reason=(
                f"both records are {incoming_kind} about the same subject; the later one "
                f"stands: {winner.id} replaces {loser.id}"
            ),
        )
    return Resolution(
        kind=ResolutionKind.CONFLICT,
        incoming=incoming.id,
        incumbent=incumbent.id,
        subject=subject,
        winner=None,
        reason=(
            f"{incoming.id} and {incumbent.id} are both {incoming_kind} about one subject "
            f"with nothing to choose between them, so both are kept as conflicted"
        ),
    )


def related_to(
    records: Sequence[MemoryRecord],
    record: MemoryRecord,
    *,
    threshold: float = SUBJECT_SIMILARITY_THRESHOLD,
) -> tuple[Resolution, ...]:
    """Same tenant, scope, scope_ref and type, a similar subject, a different key."""
    key = conflict_key(record)
    return tuple(
        Resolution(
            kind=ResolutionKind.RELATED,
            incoming=record.id,
            incumbent=other.id,
            subject=record.subject,
            winner=None,
            reason=(
                f"{record.id} and {other.id} are both {record.type} about similar subjects "
                f"({record.subject!r} / {other.subject!r}); reported, not resolved"
            ),
        )
        for other in records
        if other.id != record.id
        and conflict_key(other) != key
        and conflict_key(other)[:4] == key[:4]
        and subjects_are_related(other.subject, record.subject, threshold=threshold)
    )


def plan_resolution(
    record: MemoryRecord, siblings: Sequence[MemoryRecord], *, now: datetime
) -> tuple[tuple[MemoryRecord, ...], tuple[Resolution, ...]]:
    """What to store, and what to report, when an admitted record meets its siblings.

    The decisions are :func:`resolve_against` and :func:`related_to`; what is decided
    *here* is the order to apply them in, which is what somebody reading
    ``memory/resolved`` afterwards has to reconstruct. Both stores call this and only
    persist the result, so the two cannot resolve the same pair differently:

    1. **If any sibling outranks the newcomer**, the newcomer is the one that goes to
       history, pointed at by the strongest of them. It resolves nothing else: a
       record that is not the current belief cannot settle another.
    2. **Otherwise, if any sibling ties with it**, both sides become ``CONFLICTED``
       and neither is chosen. Siblings the newcomer outranks are left alone in this
       case — marking them ``SUPERSEDED`` by a record that is itself in doubt would
       move the doubt into their status.
    3. **Otherwise the newcomer stands**, and every sibling it outranks becomes
       ``SUPERSEDED`` by it, with its own ``supersedes`` naming the strongest.

    Returns the records that changed — including ``record`` when it did — and one
    :class:`Resolution` per comparison that decided something. The caller stores the
    records and writes the resolutions to the log.
    """
    if not siblings:
        return (), ()
    compared = [(other, resolve_against(other, record, now=now)) for other in siblings]
    losses = [pair for pair in compared if pair[1].kind is ResolutionKind.SUPERSEDED]
    ties = [pair for pair in compared if pair[1].kind is ResolutionKind.CONFLICT]
    wins = [pair for pair in compared if pair[1].kind is ResolutionKind.SUPERSEDES]
    if losses:
        best, resolution = max(losses, key=lambda pair: strength_of(pair[0]))
        return (supersede_record(record, by=best.id, now=now),), (resolution,)
    if ties:
        changed = [mark_conflicted(record, now=now)]
        changed.extend(mark_conflicted(other, now=now) for other, _ in ties)
        return tuple(changed), tuple(resolution for _, resolution in ties)
    if wins:
        changed = tuple(supersede_record(other, by=record.id, now=now) for other, _ in wins)
        best, _ = max(wins, key=lambda pair: strength_of(pair[0]))
        return (
            (note_replacement(record, replaces=best.id, now=now), *changed),
            tuple(resolution for _, resolution in wins),
        )
    return (), ()


def supersede_record(record: MemoryRecord, *, by: MemoryId, now: datetime) -> MemoryRecord:
    """The record lost: it keeps its text and its evidence and stops being current."""
    superseded = replace(record, status=MemoryStatus.SUPERSEDED, superseded_by=by, updated_at=now)
    assert_transition(record.status, superseded.status)
    return superseded


def mark_conflicted(record: MemoryRecord, *, now: datetime) -> MemoryRecord:
    """Kept, and marked as one of two records that cannot both be believed."""
    conflicted = replace(record, status=MemoryStatus.CONFLICTED, updated_at=now)
    assert_transition(record.status, conflicted.status)
    return conflicted


def note_replacement(record: MemoryRecord, *, replaces: MemoryId, now: datetime) -> MemoryRecord:
    """The winner's pointer at what it replaced — the chain, read backwards."""
    return replace(record, supersedes=replaces, updated_at=now)


@dataclass(frozen=True, slots=True)
class PurgeTombstone:
    """What is left after a memory's content is deleted.

    Enough to answer "was there a memory here, and who removed it", and not enough to
    reconstruct it — which is the point: a ``REVOKED`` record is a memory the system
    still holds, and a "forget this" that leaves the text on disk has not forgotten
    anything. What was purged cannot be audited afterwards, so the tombstone is
    written where the audit lives.
    """

    memory_id: MemoryId
    content_hash: str
    subject: str
    purged_at: datetime
    approved_by: str
    tenant_id: TenantId | None = None
    reason: str = ""


def may_recall(query: RecallQuery, constraints: ExecutionConstraints) -> bool:
    """The audience check — once per query, and before anything is read.

    Every record that can match a query shares its scope and scope_ref, so this is
    the audience for the whole result set. An undefined audience raises (inside
    :func:`audience_repo`) rather than returning nothing: "no memories" and "nobody
    has said who may read these" are different answers and must not look alike.
    """
    return constraints.allows_repo(audience_repo(query.scope, query.scope_ref))


#: Words too common to say anything about what a query is *about*. Kept tiny and
#: deliberately not an English stop-word list: this codebase's memories and
#: questions are English today, but a list like this is a language decision, and
#: the short one is the one that can be defended.
_QUERY_NOISE = frozenset(
    {"a", "an", "and", "are", "for", "how", "is", "it", "of", "on", "the", "to", "what", "why"}
)


def text_tokens(text: str) -> tuple[str, ...]:
    """The words a query or a memory text is matched on.

    The same folding :func:`normalize_subject` applies — NFKC, casefold, split on
    anything that is not a word character — so a question and a memory agree on
    what "the same word" means without agreeing on punctuation.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return tuple(re.findall(r"[^\W_]+", folded, flags=re.UNICODE))


def matches_text(query_text: str, record_text: str) -> bool:
    """Whether a query is *about* a memory — the rule whose first caller found it.

    ⚠️ **This replaces a substring test (`query.text.lower() in record.text.lower()`),
    and the reason is worth keeping.** Substring-against-the-record was written in
    Phase B against tests that passed a *phrase lifted from the memory itself*
    (``text="retry"``), which is not the shape a caller ever sends. Phase E gave the
    rule its first real caller, and the caller is a **question** — "why does the
    build take so long?" — which no memory text contains verbatim, so the old rule
    would have recalled nothing, forever, and looked like a store that was simply
    empty. A rule with no caller is not a rule that works; it is one that has never
    been asked.

    An empty query — or one made only of the noise words above — means **no text
    filter** (the store's "give me what is here" form), which is what the Phase B
    contract suite relies on. Otherwise a query matches when it shares any word with
    the record. Over-recalling is the deliberate error direction: a recalled memory
    costs a few lines of context and is labelled in the prompt, while a missed one is
    invisible.
    """
    wanted = {word for word in text_tokens(query_text) if word not in _QUERY_NOISE}
    if not wanted:
        return True
    return bool(wanted & set(text_tokens(record_text)))


def recall_verdict(
    record: MemoryRecord,
    query: RecallQuery,
    constraints: ExecutionConstraints,
    *,
    now: datetime,
) -> RecallVerdict:
    """What one stored record means to one query: the whole per-record rule."""
    if record.status not in RECALLABLE_STATUSES:
        return RecallVerdict.SKIP
    # Fail-closed on a missing tenant: `None` must not compare equal to anything,
    # and a record that somehow has no tenant is not shown to a caller who happens
    # to also have no tenant.
    if not record.tenant_id or record.tenant_id != constraints.tenant_id:
        return RecallVerdict.SKIP
    if record.expires_at is not None and record.expires_at <= now:
        # The deadline is observed here rather than swept by a timer nobody runs,
        # which is also why the field is not a decoration. Expiry is not deletion:
        # the record is kept and marked, so the audit trail survives.
        return RecallVerdict.EXPIRED
    # An *active* record whose provenance carries no endorsement is a
    # contradiction: `write()` refuses that provenance outright. The check stays
    # for that case — and it used to be `if not record.provenance.is_recallable`,
    # which caught something quite different. Every provisional record has a
    # non-recallable kind by construction (`admit_provisional` refuses an
    # endorsement), so the old form made `MemoryStatus.PROPOSED` — a status
    # `RECALLABLE_STATUSES` lists as recallable — unreachable by any writer. A
    # status that can never be returned is the "looks enforced, is not" shape in
    # reverse, and it was found by the first renderer that tried to show a proposal.
    if record.status is MemoryStatus.ACTIVE and not record.provenance.is_recallable:
        return RecallVerdict.SKIP
    if record.candidate.scope != query.scope or record.candidate.scope_ref != query.scope_ref:
        return RecallVerdict.SKIP
    # Where it was learned is a second, independent limit on who may see it. It can
    # only ever narrow the result — never substitute for the audience check.
    learned_in = record.provenance.repo
    if learned_in is not None and not constraints.allows_repo(learned_in):
        return RecallVerdict.SKIP
    if not matches_text(query.text, record.candidate.text):
        return RecallVerdict.SKIP
    return RecallVerdict.HIT


@dataclass(frozen=True, slots=True)
class PruneReport:
    """What :meth:`MemoryStore.prune` did, in one object.

    ``removed`` is ids, not records: the caller writes them to the log, and a
    report that carried the text would make "pruned" indistinguishable from
    "still believed" in the one place that is supposed to say otherwise.
    """

    cutoff: datetime
    removed: tuple[MemoryId, ...] = ()
    kept: int = 0
    dry_run: bool = False

    @property
    def count(self) -> int:
        return len(self.removed)


@runtime_checkable
class MemoryStore(Protocol):
    async def write(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        """Admit a candidate as recallable memory, at ``ACTIVE``.

        Refuses, rather than downgrading, in four cases:

        * ``provenance.kind`` is not recallable — a model's guess is never fact;
        * ``provenance.evidence_refs`` is empty — a trust level is a claim *about
          evidence*, so a record citing none is an assertion wearing a label;
        * the candidate's ``scope_ref`` cannot be attributed (see
          :func:`audience_repo`);
        * the candidate names no ``tenant_id`` (see :class:`UnattributableTenant`).

        Confidence is *computed* here by :func:`confidence_for`, and a candidate
        whose ``content_hash`` already exists is not written twice — the id of the
        record that is already there comes back instead, because a distillation
        pass that runs again must not double the store.

        A quarantine path exists for everything refused; silently accepting it
        here would defeat the rule, and silently downgrading would hide it.
        """
        ...

    async def admit_provisional(
        self, candidate: MemoryCandidate, provenance: Provenance
    ) -> MemoryId:
        """Admit something nothing endorses, as ``PROPOSED`` and ``LOW``.

        The honest alternative to a refused write. A model's inference is often
        worth keeping — it is just not worth *believing*, and the difference is
        carried by the status rather than by the wording of the text. The
        parameters are forced, not defaulted: a caller cannot pass ``ACTIVE`` and
        cannot pass a confidence, so there is no argument to get wrong.

        Refused anyway when the provenance *does* carry evidence: that candidate
        has an endorsement, and quietly filing it as a proposal would lose it.
        """
        ...

    async def quarantine(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        """Store a candidate that must never be recalled as fact."""
        ...

    async def confirm(self, memory_id: MemoryId, evidence: EventRef) -> MemoryRecord:
        """A human endorsed a ``PROPOSED`` record: it becomes ``ACTIVE``/``HIGH``.

        The evidence is required for the same reason it is required at admission.
        ``HUMAN_VERIFIED`` is a claim about a person's answer, and the ref is where
        that answer is written down. Returns the record as it now stands.
        """
        ...

    async def recall(
        self, query: RecallQuery, constraints: ExecutionConstraints
    ) -> Sequence[MemoryRecord]:
        """Return what this caller may see, or ``()``, or raise.

        Three failure modes that must not be confused:

        * the caller cannot reach the queried audience — returns ``()``. That is
          a permission outcome, and the ordinary one.
        * the caller's tenant is not the record's — skipped, for the same reason
          and in the same direction as a repository mismatch.
        * the queried audience is **undefined** — raises
          :class:`UnattributableScope`. That is a design gap, and an empty result
          would misrepresent it as "there is nothing here".

        ``PROPOSED`` records **are** returned, and the caller must render them as
        proposals (:attr:`MemoryRecord.is_verified` says which is which). Hiding
        them would make distillation's output unreachable; returning them as fact
        is what ``write`` refuses to do. Superseded, revoked and expired records
        are not returned — they are history.
        """
        ...

    async def revoke(self, memory_id: MemoryId) -> None:
        """Stop returning a memory, keeping it and its evidence in the audit.

        The soft half of forgetting, and the *only* half a session may do on its
        own: the record is still there to be explained afterwards, which is what
        makes it reversible and what makes it not a deletion. The caller is
        ``runtime/memory_lifecycle.py::forget``, which asks for
        ``Operation.FORGET_MEMORY`` first; calling this directly is a store-level
        act with no authorisation behind it, which is why nothing in ``src/`` does.

        Raises ``KeyError`` for an id that is not admitted, like :meth:`confirm` and
        :meth:`purge`: a "forget" that silently did nothing is indistinguishable
        from one that worked, and the two are reported to a person differently.
        """
        ...

    async def reconcile(
        self, memory_id: MemoryId, *, now: datetime | None = None
    ) -> tuple[Resolution, ...]:
        """Compare a record with the ones already on its subject, and act on the answer.

        Called right after an admission, and returns what each comparison decided —
        the *caller* writes ``memory/resolved``, because a store has no log.

        The comparisons, in one place so that a second store cannot decide them
        differently (all of them :func:`resolve_against`, plus :func:`related_to` for
        the fuzzy report):

        * a same-key record this one outranks becomes ``SUPERSEDED`` by it;
        * a same-key record that outranks this one makes *this* record
          ``SUPERSEDED``, and the caller is told, because a memory that arrived and
          immediately lost is something a person may need to see;
        * a tie marks **both** ``CONFLICTED`` and chooses neither;
        * similar-subject records under a different key are reported as
          ``RELATED`` and changed in no way.

        A no-op for a record that is not ``ACTIVE`` (only an admission is being
        judged here) and for one with no siblings. Returns ``()`` in both cases,
        rather than an empty "resolution", so that "nothing was compared" is not
        written into the log as a decision.
        """
        ...

    async def purge(
        self,
        memory_id: MemoryId,
        *,
        approved_by: str,
        reason: str = "",
        now: datetime | None = None,
    ) -> PurgeTombstone:
        """Delete a memory's content, keeping only proof that it existed.

        The one destructive operation in the system, and destructive on purpose: a
        record that is merely ``REVOKED`` is a memory the system still holds, so
        "forget this" that leaves the text on disk has not forgotten it. What
        survives is a :class:`PurgeTombstone` — the content hash, when, who approved
        it and why — enough to say "there was a memory here and it was removed", and
        not enough to reconstruct it.

        ``approved_by`` is not optional and is not inferred: this is the operation
        whose whole cost is the loss of the audit trail, so the record must name who
        accepted that cost. The caller is ``runtime/memory_lifecycle.py::purge``,
        behind ``Operation.PURGE_MEMORY`` and a typed confirmation.
        """
        ...

    async def prune(
        self,
        *,
        now: datetime | None = None,
        older_than_days: int = 90,
        dry_run: bool = False,
    ) -> PruneReport:
        """Delete rows that are history and older than ``older_than_days``.

        The counterweight to "nothing prunes old rows", and deliberately the
        narrowest version of it: only :data:`PRUNABLE_STATUSES` — superseded,
        revoked, expired — so no row this deletes could have been recalled, and an
        ``ACTIVE`` record is never aged out (a convention does not get more false by
        being old; what *does* get stale carries an ``expires_at`` and reaches
        ``EXPIRED`` through a recall that observes it).

        Deletion, not another status: the three statuses it accepts are exactly the
        terminal ones, and a fourth would be a lie about what the row is. The audit
        survives in the session log, which is where "what was believed, and when"
        lives; the store is the current state of belief, and keeping every version of
        it forever is a leak with a growing index.

        Returns what was removed so the caller can log it. A store has no log, for
        the same reason :meth:`reconcile` returns its resolutions.
        """
        ...
