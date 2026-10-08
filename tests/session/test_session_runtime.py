"""The session runtime: buffering, sequence numbers, crash recovery, fork.

The behaviours worth defending here are the ones a plausible implementation gets
wrong in a way nobody notices until a session is replayed:

- events are buffered, but sequence numbers are assigned *eagerly* (a
  model-visible message records its ``source_event_seq``, so the seq must exist
  before the flush that persists it);
- a crash mid-turn is **closed**, not dropped — the turn really happened, and
  discarding it would make the log disagree with what the model was told;
- a torn tail is **discarded**, because it was never a record.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from forgepilot.runtime.events import is_balanced, open_turn, pending_tool_calls
from forgepilot.runtime.session import Session
from forgepilot.seams.refs import SessionId, TurnId
from forgepilot.seams.session import (
    EventKind,
    SessionEvent,
    SessionHeader,
    SurfaceOp,
)


async def test_appends_are_buffered_until_flush(created) -> None:
    """Buffering is why a session does not fsync per event."""
    store, sid = created
    session = await Session.open(store, sid)

    await session.user_message(TurnId(1), "hello")

    assert session.has_pending
    assert session.durable_seq == 0, "nothing is durable until flush"
    assert await store.high_water(sid) == 0

    await session.flush()

    assert not session.has_pending
    assert session.durable_seq == 1
    assert await store.high_water(sid) == 1


async def test_sequence_numbers_are_assigned_before_the_flush(created) -> None:
    """A model-visible message records its seq, so the seq must exist up front."""
    store, sid = created
    session = await Session.open(store, sid)

    first = await session.user_message(TurnId(1), "a")
    second = await session.user_message(TurnId(1), "b")

    assert (first.seq, second.seq) == (1, 2)
    assert session.has_pending, "and they are still only buffered"


async def test_reopening_continues_the_sequence(created) -> None:
    store, sid = created
    session = await Session.open(store, sid)
    await session.user_message(TurnId(1), "first")
    await session.flush()

    reopened = await Session.open(store, sid)
    third = await reopened.user_message(TurnId(1), "second")
    await reopened.flush()

    assert third.seq == 2


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------
async def test_an_open_turn_is_closed_not_dropped(created) -> None:
    """A crash mid-turn leaves real history. It gets closed, not erased."""
    store, sid = created
    await store.append(
        sid,
        [
            SessionEvent(seq=1, kind=EventKind.TURN_START, data={"turn": 1}),
            SessionEvent(
                seq=2,
                kind=EventKind.USER_MESSAGE,
                data={"text": "did the build pass?"},
                surface=SurfaceOp.APPEND,
            ),
        ],
    )

    session = await Session.open(store, sid)

    events = session.events
    assert events[1].data["text"] == "did the build pass?", "history is preserved"
    assert events[-1].kind == EventKind.TURN_END
    assert events[-1].data["reason"] == "interrupted"
    assert is_balanced(events)


async def test_an_unanswered_tool_call_gets_a_synthetic_result(created) -> None:
    """The model was told a tool was called; the log must say what came of it."""
    store, sid = created
    await store.append(
        sid,
        [
            SessionEvent(seq=1, kind=EventKind.TURN_START, data={"turn": 1}),
            SessionEvent(
                seq=2,
                kind=EventKind.TOOL_CALL,
                data={"callId": "c1", "name": "run_tests", "arguments": "{}"},
                turn=TurnId(1),
            ),
        ],
    )

    session = await Session.open(store, sid)

    results = [e for e in session.events if e.kind == EventKind.TOOL_RESULT]
    assert len(results) == 1
    assert results[0].data["callId"] == "c1"
    assert results[0].data["isError"] is True
    assert pending_tool_calls(session.events) == ()
    assert open_turn(session.events) is None


async def test_a_balanced_log_is_left_alone(created) -> None:
    store, sid = created
    await store.append(
        sid,
        [
            SessionEvent(seq=1, kind=EventKind.TURN_START, data={"turn": 1}),
            SessionEvent(
                seq=2,
                kind=EventKind.USER_MESSAGE,
                data={"text": "hi"},
                surface=SurfaceOp.APPEND,
            ),
            SessionEvent(
                seq=3, kind=EventKind.TURN_END, data={"turn": 1, "reason": "completed"}
            ),
        ],
    )

    session = await Session.open(store, sid)

    assert len(session.events) == 3, "nothing is appended to a log that is already balanced"
    assert session.high_water == 3


# ---------------------------------------------------------------------------
# Fork
# ---------------------------------------------------------------------------
async def test_fork_carries_lineage_and_marks_the_seed_boundary(created) -> None:
    store, sid = created
    session = await Session.open(store, sid)
    await session.turn_start(TurnId(1))
    await session.user_message(TurnId(1), "original")
    await session.turn_end(TurnId(1), "completed")
    await session.flush()

    forked = await Session.fork(store, sid, tenant="acme")

    assert forked.header.parent_session == sid
    assert forked.header.seed_boundary == 3
    kinds = [e.kind for e in forked.events]
    assert kinds[-1] == EventKind.END_SEED, "the seed must be delimited"
    assert kinds.count(EventKind.END_SEED) == 1


async def test_forked_session_continues_its_own_sequence(created) -> None:
    store, sid = created
    session = await Session.open(store, sid)
    await session.turn_start(TurnId(1))  # seq 1
    await session.turn_end(TurnId(1), "completed")  # seq 2
    await session.flush()

    forked = await Session.fork(store, sid, tenant="acme")
    # Seed is seq 1..2, the end-seed marker takes 3, so live work starts at 4.
    assert forked.header.seed_boundary == 2
    assert [e.seq for e in forked.events] == [1, 2, 3]

    event = await forked.user_message(TurnId(2), "a new direction")
    await forked.flush()

    assert event.seq == 4


# ---------------------------------------------------------------------------
# Live observers — what the HTTP/SSE layer tails
# ---------------------------------------------------------------------------
async def test_a_listener_sees_events_as_they_are_appended(created) -> None:
    """Not at flush time.

    A reader that waited for durability would lag a whole step behind — and the
    approval prompt is exactly the case where the reader *is* the thing that has
    to answer, so a lagging reader is a hung turn rather than a slow one.
    """
    store, sid = created
    session = await Session.open(store, sid)
    seen: list[int] = []
    session.subscribe(lambda e: seen.append(e.seq))

    await session.turn_start(TurnId(1))
    await session.user_message(TurnId(1), "hello")

    assert seen == [1, 2], "notified on append"
    assert session.durable_seq == 0, "and still nothing is durable"


async def test_unsubscribing_stops_delivery_and_is_idempotent(created) -> None:
    store, sid = created
    session = await Session.open(store, sid)
    seen: list[int] = []
    unsubscribe = session.subscribe(lambda e: seen.append(e.seq))

    await session.turn_start(TurnId(1))
    unsubscribe()
    unsubscribe()  # A second call is not an error.
    await session.turn_end(TurnId(1), "completed")

    assert seen == [1]


async def test_a_broken_listener_cannot_break_the_turn_it_watches(created) -> None:
    """An observer is a bystander. If it could raise into `append`, a UI bug
    would become a lost turn."""
    store, sid = created
    session = await Session.open(store, sid)
    good: list[int] = []

    def explode(event: SessionEvent) -> None:
        raise RuntimeError("the UI exploded")

    session.subscribe(explode)
    session.subscribe(lambda e: good.append(e.seq))

    event = await session.turn_start(TurnId(1))  # Must not raise.

    assert event.seq == 1
    assert good == [1], "one bad listener does not silence the others"


async def test_listeners_run_inline_on_the_writing_task(created) -> None:
    """The contract the SSE bridge is built around.

    Notifications happen *inside* ``append``, on the task doing the writing, so a
    listener that writes to a socket inline would stall the turn it is watching.
    That is why the HTTP layer hands each event to a queue and does the socket
    work elsewhere — this test exists so that hand-off is not mistaken for
    politeness that can be optimised away.
    """
    store, sid = created
    session = await Session.open(store, sid)
    order: list[str] = []
    session.subscribe(lambda e: order.append(f"notified:{e.seq}"))

    await session.turn_start(TurnId(1))
    order.append("after-append-returned")

    assert order == ["notified:1", "after-append-returned"]
