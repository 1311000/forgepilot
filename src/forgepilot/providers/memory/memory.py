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
from datetime import UTC, datetime
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
    audience_repo,
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
        if not provenance.evidence_refs:
            # A trust level is a claim *about evidence*. With none cited, the level
            # is decoration — and decoration is worse than an untrusted record,
            # because everything downstream believes the label. The same argument
            # as refusing an unsigned ExecutionConstraints: omitting the evidence
            # must not be the cheap way past the evidence requirement.
            raise UnprovenMemoryWrite(
                f"refusing to admit a {provenance.kind.value!r} candidate that cites no "
                f"evidence. A record with no evidence is an assertion, not a memory — "
                f"cite the session seq, test id or ticket that supports it, or use "
                f"quarantine() to keep it without believing it."
            )
        memory_id = self._next_id("mem")
        self._records[memory_id] = MemoryRecord(
            id=memory_id,
            candidate=candidate,
            provenance=provenance,
            created_at=datetime.now(UTC),
        )
        return memory_id

    async def quarantine(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        memory_id = self._next_id("quar")
        self._quarantined[memory_id] = MemoryRecord(
            id=memory_id,
            candidate=candidate,
            provenance=provenance,
            created_at=datetime.now(UTC),
        )
        return memory_id

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
            # Where it was learned is a second, independent limit on who may see
            # it. It can only ever narrow the result — never substitute for the
            # audience check above.
            learned_in = record.provenance.repo
            if learned_in is not None and not constraints.allows_repo(learned_in):
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
