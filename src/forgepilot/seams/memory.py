"""Long-term-memory seam — service name ``memory``.

The design draws a hard line: **a session records what happened; memory holds
what is worth reusing.** Two rules follow, and both are enforced structurally
here rather than left to discipline:

- Not all history becomes memory.
- A model's unverified assertion never becomes fact. ``MemoryStore.write``
  rejects :attr:`ProvenanceKind.MODEL_ASSERTED` outright; such candidates can
  only go to :meth:`MemoryStore.quarantine`, and ``recall`` never returns them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable

from forgepilot.seams.refs import MemoryId, RepoRef, SessionId
from forgepilot.seams.security import ExecutionConstraints

__all__ = [
    "SERVICE_NAME",
    "MemoryScope",
    "ProvenanceKind",
    "Provenance",
    "MemoryCandidate",
    "MemoryRecord",
    "RecallQuery",
    "MemoryStore",
]

SERVICE_NAME = "memory"


class MemoryScope(StrEnum):
    REPO = "repo"
    PROJECT = "project"
    TEAM = "team"
    ORG = "org"


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

        Raises if ``provenance.kind`` is not recallable. A quarantine path exists
        for exactly that case; silently downgrading here would defeat the rule.
        """
        ...

    async def quarantine(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        """Store a candidate that must never be recalled as fact."""
        ...

    async def recall(
        self, query: RecallQuery, constraints: ExecutionConstraints
    ) -> Sequence[MemoryRecord]: ...

    async def revoke(self, memory_id: MemoryId) -> None: ...
