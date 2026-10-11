"""In-memory session store — service ``sessionPersistence``.

Keeps the same guarantees as the JSONL store (contiguous sequence validation,
lossless payloads, refusal to fork inside an open turn) without touching the
disk, so tests can exercise the runtime without temp directories.

It validates sequence numbers rather than assigning them, exactly as the durable
store does: a contract that only one provider honours is not a contract, and the
tests would then be testing the mock rather than the seam.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.runtime.events import open_turn, pending_tool_calls
from forgepilot.seams.refs import SessionId
from forgepilot.seams.session import (
    SERVICE_NAME,
    EventKind,
    ForkBoundaryInvalid,
    LossyEventPayload,
    NonContiguousSequence,
    Page,
    SessionEvent,
    SessionHeader,
    SessionSummary,
    TailRecovery,
)

__all__ = ["InMemorySessionStorePlugin"]


class InMemorySessionStorePlugin(PluginService):
    """Serves ``sessionPersistence`` from process memory."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._events: dict[SessionId, list[SessionEvent]] = {}
        self._headers: dict[SessionId, SessionHeader] = {}

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _assert_serialisable(event: SessionEvent) -> None:
        try:
            json.dumps(event.to_json())
        except (TypeError, ValueError) as exc:
            raise LossyEventPayload(
                f"event {event.kind!r} carries a payload that cannot be stored "
                f"losslessly, so 'model-visible means logged' would already be "
                f"broken: {exc}"
            ) from exc

    # -- SessionStore ------------------------------------------------------
    async def create(self, header: SessionHeader) -> None:
        self._events.setdefault(header.session_id, [])
        self._headers.setdefault(header.session_id, header)

    async def header(self, session_id: SessionId) -> SessionHeader:
        try:
            return self._headers[session_id]
        except KeyError:
            raise FileNotFoundError(f"no session {session_id!r}") from None

    async def append(
        self, session_id: SessionId, events: Sequence[SessionEvent]
    ) -> Sequence[SessionEvent]:
        bucket = self._events.setdefault(session_id, [])
        for offset, event in enumerate(events, start=1):
            self._assert_serialisable(event)
            expected = len(bucket) + offset
            if event.seq != expected:
                raise NonContiguousSequence(
                    f"event {event.seq} does not continue the log at {expected}; the log "
                    f"has {len(bucket)} events. Sequence numbers are validated here, not "
                    f"renumbered."
                )
        bucket.extend(events)
        return tuple(events)

    async def read(self, session_id: SessionId, *, after: int = 0) -> AsyncIterator[SessionEvent]:
        for event in list(self._events.get(session_id, ())):
            if event.seq > after:
                yield event

    async def high_water(self, session_id: SessionId) -> int:
        bucket = self._events.get(session_id, ())
        return bucket[-1].seq if bucket else 0

    async def flush(self, session_id: SessionId) -> None:
        # Nothing is buffered here; the process memory *is* the store.
        return None

    async def recover_tail(self, session_id: SessionId) -> TailRecovery:
        # An in-memory log cannot have a torn tail: events are whole objects.
        return TailRecovery(last_valid_seq=await self.high_water(session_id))

    async def fork(
        self,
        source: SessionId,
        *,
        boundary: int | None = None,
        child_session_id: SessionId | None = None,
        tenant: str = "",
        principal: str = "",
    ) -> SessionHeader:
        source_header = await self.header(source)
        seed = [e async for e in self.read(source)]
        if boundary is not None:
            seed = [e for e in seed if e.seq <= boundary]

        if open_turn(seed) is not None or pending_tool_calls(seed):
            raise ForkBoundaryInvalid(
                f"cannot fork {source!r} at this boundary: the prefix ends inside an open "
                f"turn or with unanswered tool calls."
            )

        child = child_session_id or SessionId(f"{source}-fork")
        header = SessionHeader(
            session_id=child,
            created_at=datetime.now(UTC),
            tenant=tenant or source_header.tenant,
            principal=principal or source_header.principal,
            cwd=source_header.cwd,
            parent_session=source,
            seed_boundary=seed[-1].seq if seed else 0,
        )
        await self.create(header)
        if seed:
            await self.append(child, [replace(e, tenant=header.tenant) for e in seed])
        await self.append(
            child,
            [
                SessionEvent(
                    seq=(seed[-1].seq if seed else 0) + 1,
                    kind=EventKind.END_SEED,
                    data={},
                    ts=datetime.now(UTC),
                    tenant=header.tenant,
                    principal=header.principal,
                )
            ],
        )
        return header

    async def summaries(self, *, limit: int = 50, cursor: str | None = None) -> Page:
        start = int(cursor) if cursor else 0
        entries = list(self._events.items())[start : start + limit]
        items = tuple(
            SessionSummary(
                session_id=sid,
                created_at=self._headers.get(sid, SessionHeader(sid, datetime.now(UTC))).created_at,
                last_event_at=events[-1].ts if events else None,
                event_count=len(events),
                tenant=self._headers[sid].tenant if sid in self._headers else "",
            )
            for sid, events in entries
        )
        next_cursor = str(start + limit) if start + limit < len(self._events) else None
        return Page(items=items, next_cursor=next_cursor)

    async def delete(self, session_id: SessionId) -> None:
        self._events.pop(session_id, None)
        self._headers.pop(session_id, None)
