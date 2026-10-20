"""Shutdown: the streams end, and nothing new starts.

The property under test is the one a page with an open event stream makes
visible: **``Ctrl-C`` must not hang.** uvicorn waits for in-flight connections
before it runs the ASGI lifespan shutdown, and an SSE stream has no reason to
finish, so the flag that ends it has to be raised *before* that wait. These tests
raise it directly and check the two consequences — the stream stops, and new work
is refused with a status that says "another replica might work".
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi", reason="the `web` extra is not installed")


async def test_a_stream_ends_when_shutdown_is_asked_for(live_api) -> None:
    """No final event, no error frame — the response simply completes.

    That is deliberate: a client distinguishes "the server is going away" from
    "the log said something" by the stream ending, and reconnects with
    ``Last-Event-ID``. Emitting a fake event would put something in the UI the
    log never said.
    """
    created = await live_api.client.post("/sessions", headers=live_api.auth())
    session_id = created.json()["sessionId"]

    async def read_to_the_end() -> int:
        seen = 0
        async with live_api.client.stream(
            "GET", f"/sessions/{session_id}/events", headers=live_api.auth()
        ) as response:
            assert response.status_code == 200
            async for _line in response.aiter_lines():
                seen += 1
        return seen

    reader = asyncio.create_task(read_to_the_end())
    # Let the request reach the handler, so this is not "closed before it started".
    await asyncio.sleep(0.2)
    assert not reader.done()

    live_api.registry.stop()

    await asyncio.wait_for(reader, timeout=5.0)


async def test_a_new_turn_is_refused_with_503_while_stopping(api) -> None:
    created = await api.client.post("/sessions", headers=api.auth())
    session_id = created.json()["sessionId"]

    api.registry.stop()

    response = await api.client.post(
        f"/sessions/{session_id}/messages", headers=api.auth(), json={"text": "hello"}
    )

    assert response.status_code == 503, response.text
    assert "shutting down" in response.json()["error"]
    body = (await api.client.get("/metrics")).text
    assert "forgepilot_turns_started_total" not in body


async def test_a_question_waiting_on_a_human_is_released_as_unavailable(api) -> None:
    """Shutdown must not wait out a 300-second approval timeout.

    ``UNAVAILABLE`` is what every consumer already reads as "no", so the turn
    takes its ordinary refusal path and the log records a refusal rather than a
    cancellation with no reason.
    """
    from forgepilot.seams.approval import ApprovalOutcome, ApprovalRequest
    from forgepilot.seams.refs import SessionId

    request = ApprovalRequest(
        request_id="req-1",
        session_id=SessionId("s-1"),
        tool_name="open_pr",
        arguments_json="{}",
        reason="needs a human",
    )
    asking = asyncio.create_task(api.registry.ask(request))
    await asyncio.sleep(0.05)
    assert not asking.done()

    api.registry.stop()

    outcome = await asyncio.wait_for(asking, timeout=2.0)
    assert outcome is ApprovalOutcome.UNAVAILABLE


async def test_the_health_endpoint_still_answers_while_stopping(api) -> None:
    """A load balancer has to be able to see the process it is draining.

    Returning an error here would take the container out of rotation *before* the
    drain, which is the wrong order: existing streams would be cut by the proxy
    instead of ending on their own.
    """
    api.registry.stop()

    response = await api.client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_refuses_before_liveness_does(api) -> None:
    """The two probes answer different questions, and the order is the point.

    A draining process is alive and must stop receiving *new* work; answering
    ``/readyz`` with 200 while it drains is how a load balancer opens a stream on
    a process that is about to end. Both halves are asserted here so that a later
    change cannot make readiness a copy of liveness.
    """
    ready = await api.client.get("/readyz")
    assert ready.status_code == 200, ready.text
    assert ready.json() == {"status": "ready"}

    api.registry.stop()

    draining = await api.client.get("/readyz")
    assert draining.status_code == 503, draining.text
    assert draining.json()["status"] == "draining"
    assert (await api.client.get("/healthz")).status_code == 200


async def test_readiness_names_a_missing_session_store() -> None:
    """A process that cannot open a session is not ready, and says why.

    Asserted against the pure decision rather than through the app: a booted
    container always has a store, and a kernel service cannot be shadowed
    per-test, so the route alone would leave this branch unreachable.
    """
    from forgepilot.httpapi.app import readiness

    status, body = readiness(stopping=False, store=None)

    assert status == 503
    assert "session store" in body["reason"]
    assert readiness(stopping=False, store=object()) == (200, {"status": "ready"})
