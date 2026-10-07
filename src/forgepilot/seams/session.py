"""Session-storage seam — service name ``sessionPersistence``.

The session log is an **append-only** event stream and the single source of
truth: the transcript, resume, fork, telemetry, the review narrative and memory
distillation all derive from it. Storage is a plugin so the medium can change
(JSONL today, SQLite later) without touching any of that.

Invariant, enforced elsewhere but shaped here: **model-visible means logged**.
Every payload must therefore be losslessly JSON-serialisable — if an event could
not be reconstructed from storage, the invariant is already broken at the point
of writing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from forgepilot.seams.refs import SessionId, TurnId

__all__ = [
    "SERVICE_NAME",
    "EventKind",
    "TURN_END_REASONS",
    "SessionEvent",
    "SessionSummary",
    "Page",
    "SessionStore",
]

SERVICE_NAME = "sessionPersistence"


class EventKind:
    """Event-name vocabulary.

    Not an enum: the set is open and a plugin may emit its own namespaced events.
    The ones below are the ones the runtime itself defines.
    """

    TURN_START = "turn/start"
    TURN_END = "turn/end"
    STEP_START = "step/start"
    STEP_END = "step/end"
    USER_MESSAGE = "user/message"
    REQUEST_HEADER = "request/header"
    ASSISTANT_MESSAGE = "assistant/message"
    TOOL_CALL = "tool/call"
    TOOL_RESULT = "tool/result"
    APPROVAL_ASKED = "approval/asked"
    APPROVAL_DECIDED = "approval/decided"
    CONSTRAINT_SNAPSHOT = "constraints/snapshot"
    SESSION_FORKED = "session/forked"
    TASK_STATE = "task/state"
    MEMORY_WRITE = "memory/write"


#: Closed set. ``interrupted`` is appended by a recovery sweep for any log that
#: ends inside an open turn, so the projection always sees balanced turns.
TURN_END_REASONS = frozenset(
    {"completed", "rejected", "cancelled", "budget", "error", "interrupted"}
)


@dataclass(frozen=True, slots=True)
class SessionEvent:
    seq: int
    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)
    ts: datetime | None = None
    turn: TurnId | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "seq": self.seq,
            "kind": self.kind,
            "data": dict(self.data),
        }
        if self.ts is not None:
            payload["ts"] = self.ts.isoformat()
        if self.turn is not None:
            payload["turn"] = int(self.turn)
        return payload


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: SessionId
    created_at: datetime | None = None
    last_event_at: datetime | None = None
    event_count: int = 0
    title: str = ""


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[Any, ...] = ()
    next_cursor: str | None = None


@runtime_checkable
class SessionStore(Protocol):
    async def append(self, session_id: SessionId, events: Sequence[SessionEvent]) -> int:
        """Append events, returning the new high-water sequence number."""
        ...

    def read(self, session_id: SessionId, *, after: int = 0) -> AsyncIterator[SessionEvent]:
        """Read events after ``after``, in order."""
        ...

    async def high_water(self, session_id: SessionId) -> int: ...

    async def summaries(self, *, limit: int = 50, cursor: str | None = None) -> Page: ...

    async def delete(self, session_id: SessionId) -> None: ...
