"""The session-store contract, run against **every** store implementation.

Written at the seam rather than against one provider, because a contract that
only one implementation honours is not a contract — and a test written against
one store's private behaviour would be testing that store, not the seam.

The durable store is the reason this suite is parameterised: file-backed storage
has failure modes memory does not (torn tails, oversized lines, cross-process
writers), and those must be covered by the *same* assertions the in-memory store
satisfies, or the two will quietly diverge.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.seams.refs import SessionId
from forgepilot.seams.session import (
    CorruptLog,
    EventKind,
    ForkBoundaryInvalid,
    LossyEventPayload,
    NonContiguousSequence,
    SessionEvent,
    SessionHeader,
    SessionStore,
    SurfaceOp,
)


@pytest.fixture(params=["memory", "jsonl"])
async def store(request, tmp_path: Path):
    """Each store implementation, loaded through the container."""
    from forgepilot.config import resolve_plugin

    spec = {
        "memory": "forgepilot.providers.session_store.memory:InMemorySessionStorePlugin",
        "jsonl": "forgepilot.providers.session_store.jsonl:JsonlSessionStorePlugin",
    }[request.param]
    config = {"root": str(tmp_path / "sessions")} if request.param == "jsonl" else {}

    root = create_root()
    fiber = await root.plugin(resolve_plugin(spec), config)
    try:
        yield root.sessionPersistence
    finally:
        await dispose_fiber(fiber)


def ev(seq: int, kind: str = EventKind.USER_MESSAGE, **data) -> SessionEvent:
    return SessionEvent(seq=seq, kind=kind, data=data, surface=SurfaceOp.APPEND)


async def new_session(store, sid: str = "s-1") -> SessionId:
    session_id = SessionId(sid)
    await store.create(
        SessionHeader(session_id=session_id, created_at=datetime.now(UTC))
    )
    return session_id


# ---------------------------------------------------------------------------
# Round trip and ordering
# ---------------------------------------------------------------------------
async def test_round_trips_events_in_order(store) -> None:
    assert isinstance(store, SessionStore)
    sid = await new_session(store)

    await store.append(sid, [ev(1, text="hello"), ev(2, text="world")])

    events = [e async for e in store.read(sid)]
    assert [e.seq for e in events] == [1, 2]
    assert [e.data["text"] for e in events] == ["hello", "world"]
    assert await store.high_water(sid) == 2


async def test_read_after_returns_only_the_tail(store) -> None:
    sid = await new_session(store)
    await store.append(sid, [ev(1), ev(2), ev(3)])

    tail = [e async for e in store.read(sid, after=1)]

    assert [e.seq for e in tail] == [2, 3]


async def test_surface_and_metadata_survive_storage(store) -> None:
    """Surface op and tenant must round-trip; losing either changes behaviour."""
    sid = await new_session(store)
    original = SessionEvent(
        seq=1,
        kind=EventKind.REQUEST_HEADER,
        data={"model": "m"},
        surface=SurfaceOp.LOG_ONLY,
        ts=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        event_id="evt-1",
        tenant="acme",
        principal="alice",
    )

    await store.append(sid, [original])
    stored = [e async for e in store.read(sid)][0]

    assert stored.surface is SurfaceOp.LOG_ONLY
    assert stored.tenant == "acme"
    assert stored.principal == "alice"
    assert stored.event_id == "evt-1"
    assert stored.ts == original.ts


# ---------------------------------------------------------------------------
# Sequence numbers are validated, never silently repaired
# ---------------------------------------------------------------------------
async def test_a_gap_in_the_sequence_is_refused(store) -> None:
    sid = await new_session(store)

    with pytest.raises(NonContiguousSequence):
        await store.append(sid, [ev(2)])


async def test_replaying_an_existing_seq_is_refused(store) -> None:
    sid = await new_session(store)
    await store.append(sid, [ev(1)])

    with pytest.raises(NonContiguousSequence):
        await store.append(sid, [ev(1)])


# ---------------------------------------------------------------------------
# Lossless payloads
# ---------------------------------------------------------------------------
async def test_a_payload_that_cannot_be_stored_is_refused(store) -> None:
    """Failing here is the last cheap moment: afterwards the log lies."""
    sid = await new_session(store)

    with pytest.raises(LossyEventPayload):
        await store.append(
            sid,
            [SessionEvent(seq=1, kind=EventKind.TOOL_CALL, data={"obj": object()})],
        )


# ---------------------------------------------------------------------------
# Large payloads spill without losing anything
# ---------------------------------------------------------------------------
async def test_a_large_payload_spills_but_reads_back_identical(store) -> None:
    """Spilling must not be a lossy shortcut.

    If the model saw the full result, replay has to be able to reproduce it.
    Keeping a shortened copy instead would break the invariant the moment anyone
    replayed the session — which is exactly when it matters.
    """
    sid = await new_session(store)
    big = "x" * 200_000
    await store.append(sid, [ev(1, kind=EventKind.TOOL_RESULT, content=big)])

    back = [e async for e in store.read(sid)]

    assert back[0].data["content"] == big, "the payload must survive the round trip"


# ---------------------------------------------------------------------------
# Fork
# ---------------------------------------------------------------------------
async def test_fork_copies_a_prefix_and_records_lineage(store) -> None:
    sid = await new_session(store, "parent")
    await store.append(
        sid,
        [
            SessionEvent(seq=1, kind=EventKind.TURN_START, data={"turn": 1}),
            ev(2, text="hi"),
            SessionEvent(seq=3, kind=EventKind.TURN_END, data={"turn": 1, "reason": "completed"}),
        ],
    )

    header = await store.fork(sid, child_session_id=SessionId("child"))

    assert header.parent_session == sid
    assert header.seed_boundary == 3
    child_events = [e async for e in store.read(SessionId("child"))]
    # Seed plus the end-seed marker that delimits it.
    assert [e.kind for e in child_events] == [
        EventKind.TURN_START,
        EventKind.USER_MESSAGE,
        EventKind.TURN_END,
        EventKind.END_SEED,
    ]


async def test_fork_refuses_a_boundary_inside_an_open_turn(store) -> None:
    """Refused, not clipped: clipping would return a different history."""
    sid = await new_session(store, "parent")
    await store.append(
        sid,
        [
            SessionEvent(seq=1, kind=EventKind.TURN_START, data={"turn": 1}),
            ev(2, text="mid-turn"),
        ],
    )

    with pytest.raises(ForkBoundaryInvalid):
        await store.fork(sid, child_session_id=SessionId("child"))


async def test_fork_refuses_when_a_tool_call_has_no_result(store) -> None:
    sid = await new_session(store, "parent")
    await store.append(
        sid,
        [
            SessionEvent(seq=1, kind=EventKind.TURN_START, data={"turn": 1}),
            SessionEvent(
                seq=2,
                kind=EventKind.TOOL_CALL,
                data={"callId": "c1", "name": "shell", "arguments": "{}"},
            ),
        ],
    )

    with pytest.raises(ForkBoundaryInvalid):
        await store.fork(sid, child_session_id=SessionId("child"))


# ---------------------------------------------------------------------------
# Durable-store-only failure modes
# ---------------------------------------------------------------------------
async def test_jsonl_log_lines_are_self_contained_and_framed_with_lf(tmp_path: Path) -> None:
    """Records must be one JSON object per line, newline-separated.

    Text-mode writes on Windows would turn ``\\n`` into ``\\r\\n`` and corrupt the
    framing — a difference that only shows up when the log is read on another
    platform.
    """
    from forgepilot.config import resolve_plugin

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.session_store.jsonl:JsonlSessionStorePlugin"),
        {"root": str(tmp_path / "s")},
    )
    try:
        store = root.sessionPersistence
        sid = await new_session(store, "framed")
        await store.append(sid, [ev(1, text="a"), ev(2, text="b")])
        await store.flush(sid)

        files = list((tmp_path / "s").glob("*/*/framed.jsonl"))
        assert len(files) == 1
        raw = files[0].read_bytes()
        assert b"\r\n" not in raw, "CRLF in the log means text-mode writes leaked in"
        lines = [line for line in raw.split(b"\n") if line]
        assert len(lines) == 2
        assert [json.loads(line)["seq"] for line in lines] == [1, 2]
    finally:
        await dispose_fiber(fiber)


async def test_jsonl_torn_tail_is_discarded_not_parsed(tmp_path: Path) -> None:
    """A partially written final record was never a record."""
    from forgepilot.config import resolve_plugin

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.session_store.jsonl:JsonlSessionStorePlugin"),
        {"root": str(tmp_path / "s")},
    )
    try:
        store = root.sessionPersistence
        sid = await new_session(store, "torn")
        await store.append(sid, [ev(1, text="good")])
        await store.flush(sid)

        path = next((tmp_path / "s").glob("*/*/torn.jsonl"))
        path.write_bytes(path.read_bytes() + b'{"seq": 2, "kind": "user/mess')

        recovery = await store.recover_tail(sid)

        assert recovery.truncated_partial_tail is True
        assert recovery.last_valid_seq == 1
        assert [e.seq for e in [x async for x in store.read(sid)]] == [1]
    finally:
        await dispose_fiber(fiber)


async def test_jsonl_corruption_away_from_the_tail_is_not_repaired(tmp_path: Path) -> None:
    """A hole in the middle is not a shorter log; it is a broken one."""
    from forgepilot.config import resolve_plugin

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.session_store.jsonl:JsonlSessionStorePlugin"),
        {"root": str(tmp_path / "s")},
    )
    try:
        store = root.sessionPersistence
        sid = await new_session(store, "corrupt")
        await store.append(sid, [ev(1, text="a"), ev(2, text="b")])
        await store.flush(sid)

        path = next((tmp_path / "s").glob("*/*/corrupt.jsonl"))
        lines = path.read_bytes().split(b"\n")
        lines[0] = b'{"seq": 1, "kind"'  # damage the FIRST record, not the last
        path.write_bytes(b"\n".join(lines))

        with pytest.raises(CorruptLog):
            [e async for e in store.read(sid)]
    finally:
        await dispose_fiber(fiber)


async def test_jsonl_refuses_a_second_writer_for_one_session(tmp_path: Path) -> None:
    """Append-only correctness rests on there being one writer.

    Small appends are atomic on POSIX only, and not guaranteed at all on Windows,
    so the lease is what makes the property true rather than hopeful.

    The scenario is a *second* store over the same directory, not a second call on
    the same instance — reopening your own session is legitimate and stays
    idempotent.
    """
    from forgepilot.config import resolve_plugin
    from forgepilot.providers.session_store.jsonl import SessionLocked

    spec = resolve_plugin("forgepilot.providers.session_store.jsonl:JsonlSessionStorePlugin")
    config = {"root": str(tmp_path / "s")}

    first_root = create_root()
    first = await first_root.plugin(spec, config)
    second_root = create_root()
    second = await second_root.plugin(spec, config)
    try:
        sid = await new_session(first_root.sessionPersistence, "leased")

        with pytest.raises(SessionLocked, match="already has a writer"):
            await second_root.sessionPersistence.append(
                sid, [ev(1, text="from the second writer")]
            )
    finally:
        await dispose_fiber(first)
        await dispose_fiber(second)


async def test_jsonl_reopening_your_own_session_is_allowed(tmp_path: Path) -> None:
    """The lease guards against a second writer, not against re-entering."""
    from forgepilot.config import resolve_plugin

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.session_store.jsonl:JsonlSessionStorePlugin"),
        {"root": str(tmp_path / "s")},
    )
    try:
        store = root.sessionPersistence
        sid = await new_session(store, "reentrant")

        await store.create(
            SessionHeader(session_id=sid, created_at=datetime.now(UTC))
        )  # must not raise
        await store.append(sid, [ev(1, text="fine")])

        assert await store.high_water(sid) == 1
    finally:
        await dispose_fiber(fiber)
