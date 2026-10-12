"""In-memory memory store — service ``memory``.

This file is storage and nothing else: a dict of records, a quarantine, and an index
from content hash to id so a second distillation pass over the same conversation
cannot double the store.

**The rules live in :mod:`forgepilot.seams.memory`**, and that is the point of the
rewrite that produced the SQLite store. The four rules this provider used to state —
refuse an unendorsed write, decide confidence from the evidence rather than from the
model, filter recall on audience and tenant, refuse a candidate with no tenant — now
exist once, as functions, because a second implementation is exactly where "a rule
that is usually respected" turns into two different answers to "what may be
believed".

So the question "what stops a model's guess from being recalled as fact?" is answered
by the functions called below, and there is one of each.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.memory import (
    COMPETING_STATUSES,
    RECALLABLE_STATUSES,
    SERVICE_NAME,
    EventRef,
    MemoryCandidate,
    MemoryRecord,
    MemoryStatus,
    Provenance,
    PurgeTombstone,
    RecallQuery,
    RecallVerdict,
    Resolution,
    UnprovenMemoryWrite,
    confirm_record,
    conflict_key,
    content_hash_of,
    count_recall,
    expire_record,
    may_recall,
    new_provisional_record,
    new_quarantined_record,
    new_recallable_record,
    plan_resolution,
    promote_proposal,
    recall_verdict,
    related_to,
    require_provisional_provenance,
    require_recallable_provenance,
    require_tenant,
    revoke_record,
    siblings_of,
)
from forgepilot.seams.refs import MemoryId
from forgepilot.seams.security import ExecutionConstraints

__all__ = ["InMemoryMemoryStorePlugin", "UnprovenMemoryWrite"]


class InMemoryMemoryStorePlugin(PluginService):
    """Serves ``memory`` from process memory."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._records: dict[MemoryId, MemoryRecord] = {}
        self._quarantined: dict[MemoryId, MemoryRecord] = {}
        # content_hash -> id, so a second distillation pass over the same
        # conversation cannot double the store. Only *live* records are indexed;
        # a superseded or revoked claim may be re-asserted later.
        self._by_hash: dict[str, MemoryId] = {}
        #: What purging leaves behind (see ``PurgeTombstone``).
        self._tombstones: list[PurgeTombstone] = []
        self._seq = 0

    def _next_id(self, prefix: str) -> MemoryId:
        self._seq += 1
        return MemoryId(f"{prefix}-{self._seq}")

    # -- MemoryStore -------------------------------------------------------
    async def write(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        require_recallable_provenance(provenance)
        require_tenant(candidate)
        now = datetime.now(UTC)
        digest = content_hash_of(candidate)

        existing = self._live_record(digest)
        if existing is not None:
            # The claim is already here, so a model that phrases the same thing twice
            # must not create a second record — and a *better* provenance arriving
            # must not be silently dropped either, so a proposal is promoted in place.
            # Either way the caller gets the id of the record that stands.
            if existing.status is MemoryStatus.PROPOSED:
                self._records[existing.id] = promote_proposal(existing, provenance, now=now)
            return existing.id

        memory_id = self._next_id("mem")
        self._records[memory_id] = new_recallable_record(memory_id, candidate, provenance, now=now)
        self._by_hash[digest] = memory_id
        return memory_id

    async def admit_provisional(
        self, candidate: MemoryCandidate, provenance: Provenance
    ) -> MemoryId:
        require_provisional_provenance(provenance)
        require_tenant(candidate)
        now = datetime.now(UTC)
        digest = content_hash_of(candidate)
        existing = self._live_record(digest)
        if existing is not None:
            return existing.id

        memory_id = self._next_id("prop")
        self._records[memory_id] = new_provisional_record(
            memory_id, candidate, provenance, now=now
        )
        self._by_hash[digest] = memory_id
        return memory_id

    async def quarantine(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        memory_id = self._next_id("quar")
        self._quarantined[memory_id] = new_quarantined_record(
            memory_id, candidate, provenance, now=datetime.now(UTC)
        )
        return memory_id

    async def confirm(self, memory_id: MemoryId, evidence: EventRef) -> MemoryRecord:
        record = self._records.get(memory_id)
        if record is None:
            raise KeyError(f"no memory record {memory_id!r} is admitted")
        confirmed = confirm_record(record, evidence, now=datetime.now(UTC))
        self._records[memory_id] = confirmed
        return confirmed

    async def recall(
        self, query: RecallQuery, constraints: ExecutionConstraints
    ) -> Sequence[MemoryRecord]:
        if not may_recall(query, constraints):
            return ()
        now = datetime.now(UTC)
        hits: list[MemoryRecord] = []
        for memory_id, record in list(self._records.items()):
            verdict = recall_verdict(record, query, constraints, now=now)
            if verdict is RecallVerdict.EXPIRED:
                self._records[memory_id] = expire_record(record, now=now)
                self._drop_hash(record.content_hash)
                continue
            if verdict is not RecallVerdict.HIT:
                continue
            counted = count_recall(record, now=now)
            self._records[memory_id] = counted
            hits.append(counted)
            if len(hits) >= query.limit:
                break
        return tuple(hits)

    async def revoke(self, memory_id: MemoryId) -> None:
        record = self._records.get(memory_id)
        if record is None:
            raise KeyError(f"no memory record {memory_id!r} is admitted")
        self._records[memory_id] = revoke_record(record, now=datetime.now(UTC))
        self._drop_hash(record.content_hash)

    async def reconcile(
        self, memory_id: MemoryId, *, now: datetime | None = None
    ) -> tuple[Resolution, ...]:
        """Compare a just-admitted record with the ones already on its subject.

        The decisions are :func:`plan_resolution`; this method finds the neighbours,
        stores what the decision changed, and reports what it decided. Only an
        ``ACTIVE`` record is judged here — a proposal or a conflict is not the current
        belief, so an admission does not get to resolve the subject around it.
        """
        moment = now or datetime.now(UTC)
        record = self._records.get(memory_id)
        if record is None or record.status is not MemoryStatus.ACTIVE:
            return ()
        neighbours = tuple(
            other
            for other in self._records.values()
            if other.status in COMPETING_STATUSES
            and conflict_key(other)[:4] == conflict_key(record)[:4]
        )
        changed, decisions = plan_resolution(
            record, siblings_of(neighbours, record), now=moment
        )
        for updated in changed:
            self._store(updated)
        current = self._records.get(memory_id, record)
        return (*decisions, *related_to(neighbours, current))

    async def purge(
        self,
        memory_id: MemoryId,
        *,
        approved_by: str,
        reason: str = "",
        now: datetime | None = None,
    ) -> PurgeTombstone:
        """Delete the record's content, keeping the tombstone that says it existed."""
        moment = now or datetime.now(UTC)
        record = self._records.pop(memory_id, None)
        if record is None:
            record = self._quarantined.pop(memory_id, None)
        if record is None:
            raise KeyError(f"no memory record {memory_id!r} is admitted")
        self._drop_hash(record.content_hash)
        tombstone = PurgeTombstone(
            memory_id=memory_id,
            content_hash=record.content_hash,
            subject=record.subject,
            purged_at=moment,
            approved_by=approved_by,
            tenant_id=record.tenant_id,
            reason=reason,
        )
        self._tombstones.append(tombstone)
        return tombstone

    # -- internals ---------------------------------------------------------
    def _store(self, record: MemoryRecord) -> None:
        """The single write path for an existing record (mirrors ``sqlite._put``)."""
        self._records[record.id] = record
        if record.status not in RECALLABLE_STATUSES:
            self._drop_hash(record.content_hash)

    def _live_record(self, digest: str) -> MemoryRecord | None:
        """The recallable record for this content, if a live one exists."""
        memory_id = self._by_hash.get(digest)
        if memory_id is None:
            return None
        record = self._records.get(memory_id)
        if record is None or record.status not in RECALLABLE_STATUSES:
            self._by_hash.pop(digest, None)
            return None
        return record

    def _drop_hash(self, digest: str) -> None:
        self._by_hash.pop(digest, None)

    # -- introspection for tests ------------------------------------------
    @property
    def quarantined(self) -> tuple[MemoryRecord, ...]:
        return tuple(self._quarantined.values())

    @property
    def records(self) -> tuple[MemoryRecord, ...]:
        return tuple(self._records.values())

    @property
    def tombstones(self) -> tuple[PurgeTombstone, ...]:
        return tuple(self._tombstones)
