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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from forgepilot.errors import ForgePilotError
from forgepilot.seams.refs import MemoryId, RepoRef, SessionId
from forgepilot.seams.security import ExecutionConstraints

__all__ = [
    "SERVICE_NAME",
    "UnattributableScope",
    "audience_repo",
    "MemoryScope",
    "ProvenanceKind",
    "Provenance",
    "MemoryCandidate",
    "MemoryRecord",
    "RecallQuery",
    "MemoryStore",
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


@dataclass(frozen=True, slots=True)
class Provenance:
    kind: ProvenanceKind
    session_id: SessionId | None = None
    repo: RepoRef | None = None
    #: Pointers to the evidence: session-log sequence numbers, test ids, ticket
    #: ids. A record with no evidence is an assertion, not a memory.
    evidence_refs: tuple[str, ...] = ()

    @property
    def is_recallable(self) -> bool:
        return self.kind in RECALLABLE_PROVENANCE


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """A prospective memory, before it has been admitted."""

    text: str
    scope: MemoryScope
    scope_ref: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: MemoryId
    candidate: MemoryCandidate
    provenance: Provenance
    created_at: datetime | None = None
    supersedes: MemoryId | None = None


@dataclass(frozen=True, slots=True)
class RecallQuery:
    text: str
    scope: MemoryScope
    scope_ref: str
    limit: int = 10


@runtime_checkable
class MemoryStore(Protocol):
    async def write(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        """Admit a candidate as recallable memory.

        Refuses, rather than downgrading, in three cases:

        * ``provenance.kind`` is not recallable — a model's guess is never fact;
        * ``provenance.evidence_refs`` is empty — a trust level is a claim *about
          evidence*, so a record citing none is an assertion wearing a label;
        * the candidate's ``scope_ref`` cannot be attributed (see
          :func:`audience_repo`).

        A quarantine path exists for everything refused; silently accepting it
        here would defeat the rule, and silently downgrading would hide it.
        """
        ...

    async def quarantine(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        """Store a candidate that must never be recalled as fact."""
        ...

    async def recall(
        self, query: RecallQuery, constraints: ExecutionConstraints
    ) -> Sequence[MemoryRecord]:
        """Return what this caller may see, or ``()``, or raise.

        Two failure modes that must not be confused:

        * the caller cannot reach the queried audience — returns ``()``. That is
          a permission outcome, and the ordinary one.
        * the queried audience is **undefined** — raises
          :class:`UnattributableScope`. That is a design gap, and an empty result
          would misrepresent it as "there is nothing here".
        """
        ...

    async def revoke(self, memory_id: MemoryId) -> None: ...
