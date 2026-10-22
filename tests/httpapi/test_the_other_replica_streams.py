"""P16: a stream served by a replica that is not the writer.

Two claims, tested separately because they fail differently:

1. The store-backed tail replays the log by ``seq`` and resumes from a cursor —
   the part a browser reconnecting to the *other* replica depends on.
2. A replica that never ran a session can still read it over HTTP, from storage.

The stream is asserted at the seam rather than through ``httpx.ASGITransport``:
that transport collects a response to completion, so an endless SSE generator
would deadlock the test rather than exercise it. ``tests/httpapi/test_stream_tail.py``
takes the same route for the in-process tail, for the same reason.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from forgepilot.httpapi.app import build_app
from forgepilot.httpapi.sse import tail_store
from forgepilot.providers.llm.replay import text_turn
from forgepilot.seams.refs import SessionId


async def until(predicate, *, timeout: float = 10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the expected state never arrived in time")


@pytest.fixture
async def on_disk(tmp_path):
    """A real JSONL store, and a session written to it through ``Session``."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root
    from forgepilot.runtime.session import Session
    from forgepilot.seams.refs import TurnId

    root = create_root()
    await root.plugin(
        resolve_plugin("forgepilot.providers.session_store.jsonl:JsonlSessionStorePlugin"),
        {"root": str(tmp_path / "sessions")},
    )
    sid = SessionId("s-two-replicas")
    live = await Session.create(root.sessionPersistence, sid, tenant="acme", principal="alice")
    await live.turn_start(TurnId(1))
    await live.user_message(TurnId(1), "hello from the writer")
    await live.turn_end(TurnId(1), "completed")
    await live.flush()
    return root.sessionPersistence, sid


@pytest.mark.asyncio
async def test_a_stream_from_storage_replays_and_resumes(on_disk) -> None:
    store, sid = on_disk

    async def poll(after_seq: int):
        return [event async for event in store.read(sid, after=after_seq)]

    gen = tail_store(poll, after=0, interval_s=0.02)
    first = await asyncio.wait_for(gen.__anext__(), timeout=5)
    assert first["event"] == "stream/open", "the client must learn it is connected before an event"

    frames = []
    while len(frames) < 3:
        frames.append(await asyncio.wait_for(gen.__anext__(), timeout=5))
    await gen.aclose()

    kinds = [frame["event"] for frame in frames]
    assert kinds == ["turn/start", "user/message", "turn/end"], kinds
    assert [frame["id"] for frame in frames] == ["1", "2", "3"]

    # Resume: a cursor means "strictly after", so the same three are not resent.
    resumed = tail_store(poll, after=2, interval_s=0.02)
    await resumed.__anext__()  # stream/open
    only = await asyncio.wait_for(resumed.__anext__(), timeout=5)
    await resumed.aclose()
    assert only["event"] == "turn/end" and only["id"] == "3"


@pytest.mark.asyncio
async def test_the_other_replica_can_read_a_session_it_never_ran(api, tmp_path) -> None:
    """B's registry has no live session; the log still answers."""
    created = await api.client.post("/sessions", headers=api.auth())
    session_id = created.json()["sessionId"]
    live = api.registry.sessions[SessionId(session_id)]
    api.llm.script(text_turn("written by replica a"))
    await api.client.post(
        f"/sessions/{session_id}/messages", json={"text": "hi"}, headers=api.auth()
    )
    await until(lambda: "turn/end" in [e.kind for e in live.runtime.session.events])
    await live.runtime.session.flush()

    other = build_app(api.root, workspace_root=tmp_path / "ws", approval_timeout=5.0)
    transport = httpx.ASGITransport(app=other)
    async with httpx.AsyncClient(transport=transport, base_url="http://replica-b") as client:
        response = await client.get(f"/sessions/{session_id}", headers=api.auth())
        stranger = await client.get(
            f"/sessions/{session_id}", headers={"Authorization": "Bearer nobody"}
        )

    body = response.json()
    assert response.status_code == 200
    assert body["sessionId"] == session_id
    # The same shape as the live answer, folded from the log: a count and a price.
    assert body["events"] >= 4, body
    assert body["usage"]["calls"] == 1
    assert body["running"] is False
    assert stranger.status_code in (401, 404)
