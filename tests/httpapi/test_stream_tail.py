"""Tailing a session log — the logic behind the stream.

Tested against a real ``Session`` rather than a stub, because the interesting
failure is a *seam* between two mechanisms: the backlog is read from the log and
the live events arrive from a subscription, and the only question that matters is
whether joining them loses or duplicates anything.

It also means the cursor is exercised as what it is — the log's ``seq`` — rather
than as an opaque token the tests agree with the code about.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from forgepilot.httpapi.sse import LostStream, tail_session


@pytest.fixture
async def session(tmp_path):
    """A live session over the in-memory store, with three events already in it."""
    from forgepilot.kernel import create_root, dispose_fiber
    from forgepilot.runtime.session import Session
    from forgepilot.seams.refs import SessionId, TurnId
    from forgepilot.seams.session import SessionHeader

    root = create_root()
    fiber = await root.plugin(
        __import__(
            "forgepilot.config", fromlist=["resolve_plugin"]
        ).resolve_plugin("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin"),
        {},
    )
    try:
        sid = SessionId("s-tail")
        await root.sessionPersistence.create(
            SessionHeader(session_id=sid, created_at=datetime.now(UTC), tenant="acme")
        )
        live = await Session.open(root.sessionPersistence, sid)
        await live.turn_start(TurnId(1))
        await live.user_message(TurnId(1), "hello")
        await live.turn_end(TurnId(1), "completed")
        await live.flush()
        yield live
    finally:
        await dispose_fiber(fiber)


async def test_the_backlog_is_replayed_from_the_cursor(session) -> None:
    """A reconnect is a read, and the cursor is a sequence number."""
    stream = tail_session(session, after=1)
    try:
        frames = [await stream.__anext__(), await stream.__anext__()]
    finally:
        await stream.aclose()

    assert [f["id"] for f in frames] == ["2", "3"]
    assert [f["event"] for f in frames] == ["user/message", "turn/end"]


async def test_a_cursor_past_the_end_yields_nothing_but_stays_open(session) -> None:
    stream = tail_session(session, after=99)
    try:
        with pytest.raises(TimeoutError):
            import asyncio

            await asyncio.wait_for(stream.__anext__(), timeout=0.1)
    finally:
        await stream.aclose()


async def test_a_live_event_is_delivered_exactly_once(session) -> None:
    """The join between backlog and subscription is where a duplicate would hide.

    The backlog is snapshotted before subscribing, and the subscription only
    forwards events past the backlog's high-water mark. Getting that order wrong
    double-delivers the boundary event, and a UI would render it twice.
    """
    from forgepilot.seams.refs import TurnId

    stream = tail_session(session, after=0)
    try:
        replayed = [await stream.__anext__() for _ in range(3)]  # drain the backlog
        await session.turn_start(TurnId(2))  # seq 4, live
        live = await stream.__anext__()
        await session.user_message(TurnId(2), "again")  # seq 5, live
        also_live = await stream.__anext__()
    finally:
        await stream.aclose()

    assert [f["id"] for f in replayed] == ["1", "2", "3"]
    # The boundary event — the one already in the backlog when the subscription
    # opened — must not come round a second time.
    assert [live["id"], also_live["id"]] == ["4", "5"]


async def test_a_slow_client_is_disconnected_rather_than_silently_dropped(session) -> None:
    """A stream with a hole in it is worse than a closed one.

    A client that cannot tell it missed events renders a confident, incomplete
    account of what happened. Closing the stream makes it reconnect, and the
    cursor it already has replays the gap exactly.
    """
    from forgepilot.seams.refs import TurnId

    stream = tail_session(session, after=0, queue_limit=4)
    try:
        await stream.__anext__()  # drain the backlog
        for turn in range(1, 12):
            await session.turn_start(TurnId(100 + turn))
        # The next read is served from the queue, and the overflow is noticed
        # there — which is the point: it is reported on the same path that would
        # otherwise have delivered the missing events.
        with pytest.raises(LostStream):
            for _ in range(50):
                await stream.__anext__()
    finally:
        await stream.aclose()


async def test_closing_the_stream_unsubscribes(session) -> None:
    """An abandoned connection must not leave a listener on the session."""
    stream = tail_session(session, after=0)
    await stream.__anext__()
    assert session._listeners, "the stream should be subscribed while open"

    await stream.aclose()

    assert session._listeners == [], "the listener outlived the connection"
