"""The SQLite store's own property: the database, not the caller, decides `seq`.

Everything else it must do is covered by `tests/contract/test_session_store_contract.py`,
which now runs every store through the same assertions. What that suite cannot
express is the reason this store exists:

* a caller may leave `seq = 0` and be given the next number, and
* two *independent connections* appending to one session at once neither lose an
  event nor produce a duplicate seq.

The second is the one that matters. Two replicas that each read "the log has 6
events" and then both write 7 produce a log no code in this repository can repair,
and a test that appends from one connection in a loop would pass on the broken
implementation too — it has to be two connections.

The store is loaded through the kernel rather than constructed directly, because a
`PluginService` registers itself with its context on construction; that is also how
production reaches it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forgepilot.config import resolve_plugin
from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.seams.refs import SessionId
from forgepilot.seams.session import (
    EventKind,
    NonContiguousSequence,
    SessionEvent,
    SessionHeader,
    SurfaceOp,
)

SPEC = "forgepilot.providers.session_store.sqlite:SqliteSessionStorePlugin"


class _Handle:
    def __init__(self, store: Any, fiber: Any) -> None:
        self.store = store
        self._fiber = fiber

    async def close(self) -> None:
        closer = getattr(self.store, "close", None)
        if callable(closer):
            closer()
        await dispose_fiber(self._fiber)


async def _open(path: Path | str) -> _Handle:
    root = create_root()
    fiber = await root.plugin(resolve_plugin(SPEC), {"path": str(path)})
    return _Handle(root.sessionPersistence, fiber)


async def _session(store: Any, session_id: str = "s-1") -> SessionId:
    sid = SessionId(session_id)
    await store.create(SessionHeader(session_id=sid, created_at=datetime.now(UTC)))
    return sid


def _unassigned(kind: str = EventKind.USER_MESSAGE, **data: object) -> SessionEvent:
    """The shape a provider uses when it has no buffer to keep seq in."""
    return SessionEvent(seq=0, kind=kind, data=dict(data), surface=SurfaceOp.APPEND)


async def test_the_store_assigns_the_next_sequence_number(tmp_path: Path) -> None:
    handle = await _open(tmp_path / "s.db")
    try:
        sid = await _session(handle.store)

        first = await handle.store.append(sid, [_unassigned(text="one"), _unassigned(text="two")])
        second = await handle.store.append(sid, [_unassigned(text="three")])

        assert [event.seq for event in first] == [1, 2]
        assert [event.seq for event in second] == [3]
        assert all(event.event_id for event in list(first) + list(second)), (
            "an id is filled in, not left empty"
        )
        assert [event.data["text"] async for event in handle.store.read(sid)] == [
            "one",
            "two",
            "three",
        ]
    finally:
        await handle.close()


async def test_a_caller_supplied_gap_is_refused_not_renumbered(tmp_path: Path) -> None:
    handle = await _open(tmp_path / "s.db")
    try:
        sid = await _session(handle.store)
        await handle.store.append(sid, [_unassigned()])

        with pytest.raises(NonContiguousSequence):
            await handle.store.append(sid, [SessionEvent(seq=5, kind=EventKind.USER_MESSAGE)])

        assert await handle.store.high_water(sid) == 1, "a refused batch spends no numbers"
    finally:
        await handle.close()


async def test_two_connections_appending_interleaved_lose_nothing(tmp_path: Path) -> None:
    """The fleet case: two replicas, one session, one log."""
    path = tmp_path / "s.db"
    left = await _open(path)
    right = await _open(path)
    try:
        sid = await _session(left.store)
        await right.store.create(SessionHeader(session_id=sid, created_at=datetime.now(UTC)))

        for round_number in range(10):
            await left.store.append(sid, [_unassigned(replica="left", round=round_number)])
            await right.store.append(sid, [_unassigned(replica="right", round=round_number)])

        events = [event async for event in left.store.read(sid)]
        seqs = [event.seq for event in events]

        assert seqs == list(range(1, 21)), f"one log, contiguous: {seqs}"
        assert len(events) == 20
        assert sum(1 for event in events if event.data["replica"] == "left") == 10
        assert sum(1 for event in events if event.data["replica"] == "right") == 10
        assert await right.store.high_water(sid) == 20
    finally:
        await left.close()
        await right.close()


async def test_wal_is_on_so_readers_do_not_wait_for_the_writer(tmp_path: Path) -> None:
    handle = await _open(tmp_path / "s.db")
    try:
        mode = handle.store._db["conn"].execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        await handle.close()


async def test_recover_tail_says_there_is_nothing_to_trim(tmp_path: Path) -> None:
    handle = await _open(tmp_path / "s.db")
    try:
        sid = await _session(handle.store)
        await handle.store.append(sid, [_unassigned()])

        recovery = await handle.store.recover_tail(sid)

        assert recovery.truncated_partial_tail is False
        assert recovery.last_valid_seq == 1
        assert "no tail fragment" in recovery.detail
    finally:
        await handle.close()
