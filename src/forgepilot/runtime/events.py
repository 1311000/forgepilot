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
    "TurnSpan",
    "open_turn",
    "pending_tool_calls",
    "is_balanced",
    "model_visible_events",
    "last_turn_numbers",
    "turn_spans",
    "tool_result_seqs",
    "superseded_seqs",
    "first_user_message_seq",
    "last_seq_of_kind",
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


# ---------------------------------------------------------------------------
# Region selection — what compaction needs to know about the log's shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnSpan:
    """One turn's extent in the log, from its ``turn/start`` to its ``turn/end``."""

    turn: TurnId
    start_seq: int
    end_seq: int
    #: False when the turn never closed — a crash, or the turn in progress. A
    #: compaction must not cover an incomplete turn: it has no end to snap to.
    complete: bool


def turn_spans(events: Sequence[SessionEvent]) -> tuple[TurnSpan, ...]:
    """Every turn the log opens, in order.

    Compaction works in **whole turns**, because that is the smallest unit whose
    boundaries are already meaningful. A span that starts mid-turn can cut a
    ``tool/call`` away from its ``tool/result``, and an unanswered tool call is
    rejected outright by the endpoint — one turn later, with a traceback that
    points at the request rather than at the cut.
    """
    spans: list[TurnSpan] = []
    opened: dict[int, int] = {}
    for event in events:
        turn = event.data.get("turn")
        if event.kind == EventKind.TURN_START and turn is not None:
            opened[int(turn)] = event.seq
        elif event.kind == EventKind.TURN_END and turn is not None:
            start = opened.pop(int(turn), None)
            if start is not None:
                spans.append(
                    TurnSpan(
                        turn=TurnId(int(turn)),
                        start_seq=start,
                        end_seq=event.seq,
                        complete=True,
                    )
                )
    for turn, start in opened.items():
        spans.append(TurnSpan(turn=TurnId(turn), start_seq=start, end_seq=start, complete=False))
    spans.sort(key=lambda span: span.start_seq)
    return tuple(spans)


def tool_result_seqs(events: Sequence[SessionEvent]) -> tuple[int, ...]:
    """Seqs of ``tool/result`` events, oldest first.

    Masking targets these and only these. It never touches an
    ``assistant/message``, which is what makes it structurally impossible for a
    mask to separate a tool call from its answer.
    """
    return tuple(event.seq for event in events if event.kind == EventKind.TOOL_RESULT)


def superseded_seqs(events: Sequence[SessionEvent]) -> frozenset[int]:
    """Every seq that some ``REPLACE`` event has taken the place of.

    ⚠️ **A superseded event is still in the log, carrying its original
    ``surface``.** The log is append-only, so "replaced" is not a mutation of the
    event — it is a *later* event whose ``supersedes`` window covers it. Anything
    that walks the raw log to decide what the model can still see has to resolve
    that, and reading ``surface`` alone does not.

    Both call sites did read it alone, and the cost was measured. A second
    compaction pass re-selected results the first pass had already masked — the
    original `tool/result` still said `APPEND`, so the filter's documented
    guarantee ("a placeholder carries `REPLACE`, so a second pass cannot mask a
    mask") was true of the placeholder and false of its target. The replacement it
    emitted named a range nothing in the visible list any longer had, so
    `derive_messages` fell through to its append branch and put a `tool` message at
    the **end of the conversation**, answering a call from the start of it.
    DeepSeek refused the request outright.
    """
    covered: set[int] = set()
    for event in events:
        if event.surface is not SurfaceOp.REPLACE:
            continue
        window = event.data.get("supersedes") or ()
        if isinstance(window, (list, tuple)) and len(window) == 2:
            low, high = int(window[0]), int(window[1])
            if high >= low:
                covered.update(range(low, high + 1))
    return frozenset(covered)


def first_user_message_seq(events: Sequence[SessionEvent]) -> int | None:
    """The task brief, or ``None`` in a session that has none yet."""
    for event in events:
        if event.kind == EventKind.USER_MESSAGE and event.is_model_visible:
            return event.seq
    return None


def last_seq_of_kind(events: Sequence[SessionEvent], kind: str) -> int | None:
    """The most recent seq of ``kind``, or ``None``.

    Used for the two things a compaction must never cover: the latest
    ``task/check`` — the repair loop's retry prompt tells the model to read the
    check output "above" — and the latest ``compaction/summary``, which is the
    barrier that stops a summary being summarised again. Errors compound across
    passes, and no amount of budget makes that acceptable.
    """
    found: int | None = None
    for event in events:
        if event.kind == kind:
            found = event.seq
    return found
