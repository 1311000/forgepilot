"""Structural queries over a session log.

Pure functions over an immutable sequence of events. They answer the questions
the recovery sweep and the projection both need to ask — *is this log balanced?*
*which tool calls never got an answer?* — without either of them reimplementing
the reasoning.

Session owns *writing* events (and therefore sequence numbers); this module only
reads them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from forgepilot.seams.refs import TurnId
from forgepilot.seams.session import (
    TURN_END_REASONS,
    EventKind,
    SessionEvent,
    SurfaceOp,
)

__all__ = [
    "EventKind",
    "SurfaceOp",
    "TURN_END_REASONS",
    "PendingToolCall",
    "open_turn",
    "pending_tool_calls",
    "is_balanced",
    "model_visible_events",
    "last_turn_numbers",
]


@dataclass(frozen=True, slots=True)
class PendingToolCall:
    """A ``tool/call`` with no matching ``tool/result``."""

    call_id: str
    name: str
    turn: TurnId
    step: int | None
    seq: int


def open_turn(events: Sequence[SessionEvent]) -> TurnId | None:
    """The turn currently open at the end of the log, if any.

    A turn is open when its ``turn/start`` has no matching ``turn/end``. Only the
    tail matters: an unbalanced turn earlier in a log is corruption, and the
    recovery sweep reports it rather than papering over it.
    """
    open_at: TurnId | None = None
    for event in events:
        if event.kind == EventKind.TURN_START:
            if open_at is not None:
                # Two opens without a close between them.
                continue
            turn = event.data.get("turn")
            open_at = TurnId(int(turn)) if turn is not None else None
        elif event.kind == EventKind.TURN_END:
            open_at = None
    return open_at


def pending_tool_calls(events: Sequence[SessionEvent]) -> tuple[PendingToolCall, ...]:
    """Every tool call still awaiting a result.

    Matched by ``call_id`` rather than by position, because a parallel tool batch
    finishes out of order and position-based pairing would mis-attribute results.
    """
    calls: dict[str, PendingToolCall] = {}
    for event in events:
        if event.kind == EventKind.TOOL_CALL:
            call_id = str(event.data.get("callId", ""))
            if not call_id:
                continue
            turn = event.data.get("turn")
            calls[call_id] = PendingToolCall(
                call_id=call_id,
                name=str(event.data.get("name", "")),
                turn=TurnId(int(turn)) if turn is not None else TurnId(0),
                step=event.step,
                seq=event.seq,
            )
        elif event.kind == EventKind.TOOL_RESULT:
            calls.pop(str(event.data.get("callId", "")), None)
    return tuple(calls.values())


def is_balanced(events: Sequence[SessionEvent]) -> bool:
    """Whether the log closes every turn it opens and answers every tool call."""
    if open_turn(events) is not None:
        return False
    return not pending_tool_calls(events)


def model_visible_events(events: Sequence[SessionEvent]) -> tuple[SessionEvent, ...]:
    """Only the events the model is entitled to see, in order.

    This is the single filter the projection is built on. Keeping it here — rather
    than inline in the projection — means the question "would the model see
    this?" has one answer.
    """
    return tuple(event for event in events if event.is_model_visible)


def last_turn_numbers(events: Sequence[SessionEvent]) -> tuple[TurnId, ...]:
    """Turn ids in first-appearance order."""
    seen: list[TurnId] = []
    for event in events:
        if event.kind == EventKind.TURN_START:
            turn = event.data.get("turn")
            if turn is not None:
                seen.append(TurnId(int(turn)))
    return tuple(seen)
