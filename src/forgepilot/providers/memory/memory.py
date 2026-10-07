"""In-memory memory store — service ``memory``.

This provider is where two stated rules stop being advice and become mechanics:

- :meth:`write` **refuses** :attr:`ProvenanceKind.MODEL_ASSERTED`. A model's
  unverified guess can only reach :meth:`quarantine`, which recall never reads.
  There is no flag, no "trust me" parameter, and no downgrade path.
- :meth:`recall` filters on provenance unconditionally and *also* on the caller's
  constraints, because a memory carries project knowledge and knowledge has an
  audience.

An implementation that "usually" respects those rules is worse than none, since
callers would still believe them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.kernel.errors import ForgePilotError
from forgepilot.seams.memory import (
    RECALLABLE_PROVENANCE,
    SERVICE_NAME,
    MemoryCandidate,
    MemoryRecord,
    Provenance,
    RecallQuery,
)
from forgepilot.seams.refs import MemoryId
from forgepilot.seams.security import ExecutionConstraints

__all__ = ["InMemoryMemoryStorePlugin", "UnprovenMemoryWrite"]


class UnprovenMemoryWrite(ForgePilotError):
    """A caller tried to admit a model-asserted candidate as recallable memory."""


class InMemoryMemoryStorePlugin(PluginService):
    """Serves ``memory`` from process memory."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._records: dict[MemoryId, MemoryRecord] = {}
        self._quarantined: dict[MemoryId, MemoryRecord] = {}
        self._revoked: set[MemoryId] = set()
        self._seq = 0

    def _next_id(self, prefix: str) -> MemoryId:
        self._seq += 1
        return MemoryId(f"{prefix}-{self._seq}")

    # -- MemoryStore -------------------------------------------------------
    async def write(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        if not provenance.is_recallable:
            allowed = sorted(kind.value for kind in RECALLABLE_PROVENANCE)
            raise UnprovenMemoryWrite(
                f"refusing to admit a {provenance.kind.value!r} candidate as recallable "
                f"memory. Only {allowed} may be written; use quarantine() for anything "
                f"else. Promoting a model's guess to fact is the failure this check "
                f"exists to prevent."
            )
        memory_id = self._next_id("mem")
        self._records[memory_id] = MemoryRecord(
            id=memory_id,
            candidate=candidate,
            provenance=provenance,
            created_at=datetime.now(timezone.utc),
        )
        return memory_id

    async def quarantine(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        memory_id = self._next_id("quar")
        self._quarantined[memory_id] = MemoryRecord(
            id=memory_id,
            candidate=candidate,
            provenance=provenance,
            created_at=datetime.now(timezone.utc),
        )
        return memory_id

    async def recall(
        self, query: RecallQuery, constraints: ExecutionConstraints
    ) -> Sequence[MemoryRecord]:
        hits: list[MemoryRecord] = []
        for memory_id, record in self._records.items():
            if memory_id in self._revoked:
                continue
            if not record.provenance.is_recallable:
                continue  # belt and braces: write() should have prevented this
            if (
                record.candidate.scope != query.scope
                or record.candidate.scope_ref != query.scope_ref
            ):
                continue
            repo = record.provenance.repo
            if repo is not None and not constraints.allows_repo(repo):
                continue
            if query.text and query.text.lower() not in record.candidate.text.lower():
                continue
            hits.append(record)
            if len(hits) >= query.limit:
                break
        return tuple(hits)

    async def revoke(self, memory_id: MemoryId) -> None:
        self._revoked.add(memory_id)

    # -- introspection for tests ------------------------------------------
    @property
    def quarantined(self) -> tuple[MemoryRecord, ...]:
        return tuple(self._quarantined.values())
