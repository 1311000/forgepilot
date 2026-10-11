"""In-memory memory store — service ``memory``.

This provider is where four stated rules stop being advice and become mechanics:

- :meth:`write` **refuses** :attr:`ProvenanceKind.MODEL_ASSERTED`. A model's
  unverified guess can only reach :meth:`admit_provisional` — which forces
  ``PROPOSED``/``LOW`` and cannot be talked out of it — or
  :meth:`quarantine`, which recall never reads.
- :meth:`recall` filters on provenance, on status and on the caller's constraints,
  because a memory carries project knowledge and knowledge has an audience.
- **Confidence is computed here**, from the provenance kind and the number of
  independent events behind it. There is no parameter a caller can pass to claim a
  belief level, and the model's own number never leaves
  ``candidate.model_confidence_hint``.
- **A candidate with no tenant is refused**, not defaulted. A record nobody can
  attribute is a record every tenant can read, which is an absent check wearing
  the clothes of a weak one.

An implementation that "usually" respects those rules is worse than none, since
callers would still believe them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.kernel.errors import ForgePilotError
from forgepilot.seams.memory import (
    RECALLABLE_PROVENANCE,
    RECALLABLE_STATUSES,
    SERVICE_NAME,
    Confidence,
    EventRef,
    MemoryCandidate,
    MemoryRecord,
    MemoryStatus,
    Provenance,
    ProvenanceKind,
    RecallQuery,
    UnattributableTenant,
    assert_transition,
    audience_repo,
    confidence_for,
    content_hash_of,
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
        # content_hash -> id, so a second distillation pass over the same
        # conversation cannot double the store. Only *live* records are indexed;
        # a superseded or revoked claim may be re-asserted later.
        self._by_hash: dict[str, MemoryId] = {}
        self._seq = 0

    def _next_id(self, prefix: str) -> MemoryId:
        self._seq += 1
        return MemoryId(f"{prefix}-{self._seq}")

    def _require_tenant(self, candidate: MemoryCandidate) -> None:
        if candidate.tenant_id is None:
            raise UnattributableTenant(
                "refusing to admit a memory whose candidate names no tenant. The "
                "recall filter compares the caller's tenant with the record's, so a "
                "record without one is not merely under-classified: it passes every "
                "comparison. Set MemoryCandidate.tenant_id from the session that "
                "evidenced the memory."
            )

    # -- MemoryStore -------------------------------------------------------
    async def write(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        if not provenance.is_recallable:
            allowed = sorted(kind.value for kind in RECALLABLE_PROVENANCE)
            raise UnprovenMemoryWrite(
                f"refusing to admit a {provenance.kind.value!r} candidate as recallable "
                f"memory. Only {allowed} may be written; use admit_provisional() or "
                f"quarantine() for anything else. Promoting a model's guess to fact is "
                f"the failure this check exists to prevent."
            )
        if not provenance.evidence_refs:
            # A trust level is a claim *about evidence*. With none cited, the level
            # is decoration — and decoration is worse than an untrusted record,
            # because everything downstream believes the label. The same argument
            # as refusing an unsigned ExecutionConstraints: omitting the evidence
            # must not be the cheap way past the evidence requirement.
            raise UnprovenMemoryWrite(
                f"refusing to admit a {provenance.kind.value!r} candidate that cites no "
                f"evidence. A record with no evidence is an assertion, not a memory — "
                f"cite the session event that supports it, or use admit_provisional() "
                f"to keep it without believing it."
            )
        self._require_tenant(candidate)
        digest = content_hash_of(candidate)

        existing = self._live_record(digest)
        if existing is not None:
            # The claim is already here. A model that phrases the same thing twice
            # must not create a second record — and a *better* provenance arriving
            # must not be silently dropped either, so a proposal is promoted in
            # place. Either way the caller gets the id of the record that stands,
            # which is why the return value is always the answer to "where is this
            # memory now" rather than "what did I just create".
            if existing.status is MemoryStatus.PROPOSED:
                promoted = replace(
                    existing,
                    status=MemoryStatus.ACTIVE,
                    confidence=confidence_for(provenance.kind, provenance.evidence_refs),
                    provenance=provenance,
                    updated_at=datetime.now(UTC),
                )
                assert_transition(existing.status, promoted.status)
                self._records[existing.id] = promoted
            return existing.id

        memory_id = self._next_id("mem")
        self._records[memory_id] = MemoryRecord(
            id=memory_id,
            candidate=candidate,
            provenance=provenance,
            confidence=confidence_for(provenance.kind, provenance.evidence_refs),
            status=MemoryStatus.ACTIVE,
            tenant_id=candidate.tenant_id,
            subject=candidate.normalized_subject,
            content_hash=digest,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            expires_at=candidate.expires_at,
        )
        self._by_hash[digest] = memory_id
        return memory_id

    async def admit_provisional(
        self, candidate: MemoryCandidate, provenance: Provenance
    ) -> MemoryId:
        if provenance.is_recallable:
            raise UnprovenMemoryWrite(
                f"refusing to file a {provenance.kind.value!r} candidate as a mere "
                f"proposal. That kind claims an endorsement, whether or not it cites "
                f"one — write() is the path that decides what an endorsement is worth, "
                f"and filing it here would lose it while looking like it was kept."
            )
        self._require_tenant(candidate)
        digest = content_hash_of(candidate)
        existing = self._live_record(digest)
        if existing is not None:
            return existing.id

        memory_id = self._next_id("prop")
        self._records[memory_id] = MemoryRecord(
            id=memory_id,
            candidate=candidate,
            provenance=provenance,
            # Forced, not defaulted. There is no argument that raises these.
            confidence=Confidence.LOW,
            status=MemoryStatus.PROPOSED,
            tenant_id=candidate.tenant_id,
            subject=candidate.normalized_subject,
            content_hash=digest,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._by_hash[digest] = memory_id
        return memory_id

    async def quarantine(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        # No tenant requirement here, deliberately: a quarantined record is never
        # returned by recall, so there is no audience for it to leak to. Demanding
        # a tenant would refuse the one path whose whole purpose is to keep
        # something without believing it.
        memory_id = self._next_id("quar")
        self._quarantined[memory_id] = MemoryRecord(
            id=memory_id,
            candidate=candidate,
            provenance=provenance,
            confidence=confidence_for(provenance.kind, provenance.evidence_refs),
            status=MemoryStatus.PROPOSED,
            tenant_id=candidate.tenant_id,
            subject=candidate.normalized_subject,
            content_hash=content_hash_of(candidate),
            created_at=datetime.now(UTC),
        )
        return memory_id

    async def confirm(self, memory_id: MemoryId, evidence: EventRef) -> MemoryRecord:
        record = self._records.get(memory_id)
        if record is None:
            raise KeyError(f"no memory record {memory_id!r} is admitted")
        assert_transition(record.status, MemoryStatus.ACTIVE)
        confirmed = replace(
            record,
            status=MemoryStatus.ACTIVE,
            confidence=Confidence.HIGH,
            # A person's answer is the strongest evidence there is, and it is
            # still evidence: the ref is kept alongside whatever was cited before,
            # so the record's history does not shrink when it is upgraded.
            provenance=Provenance(
                kind=ProvenanceKind.HUMAN_VERIFIED,
                session_id=evidence.session_id,
                repo=record.provenance.repo,
                evidence_refs=(*record.provenance.evidence_refs, evidence),
            ),
            updated_at=datetime.now(UTC),
        )
        self._records[memory_id] = confirmed
        return confirmed

    async def recall(
        self, query: RecallQuery, constraints: ExecutionConstraints
    ) -> Sequence[MemoryRecord]:
        # Decided once, on the query — and *before* anything is read. Every record
        # that can match this query shares its scope and scope_ref, so this is the
        # audience for the whole result set. An undefined audience raises here
        # rather than returning nothing: "no memories" and "nobody has said who
        # may read these" are different answers and must not look alike.
        #
        # This is the check that was missing. It used to be written per record as
        # `if repo is not None and not allows_repo(repo)`, consulting
        # `provenance.repo` — a field that may be absent, or may name the
        # repository the fact was *learned in* rather than the one it is *about*.
        # Both omissions let a caller read another repository's memory.
        audience = audience_repo(query.scope, query.scope_ref)
        if not constraints.allows_repo(audience):
            return ()
        tenant = constraints.tenant_id

        now = datetime.now(UTC)
        hits: list[MemoryRecord] = []
        for memory_id, record in list(self._records.items()):
            if record.status not in RECALLABLE_STATUSES:
                continue
            # Fail-closed on a missing tenant: `None` must not compare equal to
            # anything, and a record that somehow has no tenant is not shown to a
            # caller who happens to also have no tenant.
            if not record.tenant_id or record.tenant_id != tenant:
                continue
            if record.expires_at is not None and record.expires_at <= now:
                # The deadline is observed here rather than swept by a timer
                # nobody runs, which is also why the field is not a decoration.
                # Expiry is not deletion: the record is kept, marked, and the
                # audit trail survives.
                assert_transition(record.status, MemoryStatus.EXPIRED)
                self._records[memory_id] = replace(
                    record, status=MemoryStatus.EXPIRED, updated_at=now
                )
                self._drop_hash(record.content_hash)
                continue
            if not record.provenance.is_recallable:
                continue  # belt and braces: write() should have prevented this
            if (
                record.candidate.scope != query.scope
                or record.candidate.scope_ref != query.scope_ref
            ):
                continue
            # Where it was learned is a second, independent limit on who may see
            # it. It can only ever narrow the result — never substitute for the
            # audience check above.
            learned_in = record.provenance.repo
            if learned_in is not None and not constraints.allows_repo(learned_in):
                continue
            if query.text and query.text.lower() not in record.candidate.text.lower():
                continue
            # Counting a recall is the only way to answer "was this memory ever
            # used" later, and a count nobody increments is the defect shape in
            # miniature. Only the counter and the timestamp move.
            counted = replace(
                record, recall_count=record.recall_count + 1, last_recalled_at=now
            )
            self._records[memory_id] = counted
            hits.append(counted)
            if len(hits) >= query.limit:
                break
        return tuple(hits)

    async def revoke(self, memory_id: MemoryId) -> None:
        record = self._records.get(memory_id)
        if record is None:
            return
        assert_transition(record.status, MemoryStatus.REVOKED)
        self._records[memory_id] = replace(
            record, status=MemoryStatus.REVOKED, updated_at=datetime.now(UTC)
        )
        self._drop_hash(record.content_hash)

    # -- internals ---------------------------------------------------------
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
