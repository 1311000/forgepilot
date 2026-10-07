"""In-memory session store — service ``sessionPersistence``.

Append-only, with sequence numbers assigned by the store rather than trusted from
the caller: a caller that could choose its own ``seq`` could rewrite history, and
the whole architecture derives from this log.

The JSON-serialisability check is the same one the file-backed store will make.
It lives on the write path here too, because the invariant is *model-visible
means logged* — an event that cannot survive storage has already broken it, and
finding that out at read time is too late.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.kernel.errors import ForgePilotError
from forgepilot.seams.refs import SessionId
from forgepilot.seams.session import (
    SERVICE_NAME,
    Page,
    SessionEvent,
    SessionSummary,
)

__all__ = ["InMemorySessionStorePlugin", "LossyEventPayload"]


class LossyEventPayload(ForgePilotError):
    """An event payload cannot round-trip through JSON, so it cannot be logged.

    Storing it would mean the model saw something the log cannot reconstruct,
    which is precisely the state the *model-visible means logged* invariant
    forbids. Failing at write time is the only point where that is still cheap.
    """


class InMemorySessionStorePlugin(PluginService):
    """Serves ``sessionPersistence`` from process memory."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._events: dict[SessionId, list[SessionEvent]] = {}
        self._created: dict[SessionId, datetime] = {}

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
    async def append(self, session_id: SessionId, events: Sequence[SessionEvent]) -> int:
        bucket = self._events.setdefault(session_id, [])
        self._created.setdefault(session_id, datetime.now(timezone.utc))

        for event in events:
            self._assert_serialisable(event)
            # Sequence numbers are assigned here, not taken from the caller.
            bucket.append(replace(event, seq=len(bucket) + 1))
        return len(bucket)

    async def read(self, session_id: SessionId, *, after: int = 0) -> AsyncIterator[SessionEvent]:
        for event in list(self._events.get(session_id, ())):
            if event.seq > after:
                yield event

    async def high_water(self, session_id: SessionId) -> int:
        return len(self._events.get(session_id, ()))

    async def summaries(self, *, limit: int = 50, cursor: str | None = None) -> Page:
        start = int(cursor) if cursor else 0
        items = []
        for session_id, events in list(self._events.items())[start : start + limit]:
            items.append(
                SessionSummary(
                    session_id=session_id,
                    created_at=self._created.get(session_id),
                    last_event_at=events[-1].ts if events else None,
                    event_count=len(events),
                )
            )
        next_cursor = str(start + limit) if start + limit < len(self._events) else None
        return Page(items=tuple(items), next_cursor=next_cursor)

    async def delete(self, session_id: SessionId) -> None:
        self._events.pop(session_id, None)
        self._created.pop(session_id, None)
