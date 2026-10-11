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
from dataclasses import dataclass, field
from datetime import datetime
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
    "RECALLABLE_PROVENANCE",
    "CONFIDENCE_FOR_PROVENANCE",
    "LEGAL_TRANSITIONS",
    "MEMORY_TYPE_MODES",
    "INDEPENDENT_EVIDENCE_FOR_RAISE",
    "REVOCABLE_STATUSES",
    "RECALLABLE_STATUSES",
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

    async def revoke(self, memory_id: MemoryId) -> None: ...

