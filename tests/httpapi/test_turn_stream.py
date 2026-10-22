"""A turn over HTTP: background execution, the event stream, and approval.

**These run against a real server on a loopback socket**, not ``ASGITransport``,
because ``ASGITransport`` buffers a response to completion before handing it back
— an endless event stream can never be read through it. That is worth stating
plainly: the first version of these tests hung, and the hang looked exactly like
a broken endpoint.

The assertion this file exists for is the ordering one. Everything the pipeline
guarantees about *when* things happen — nothing effectful before approval, the
tool body strictly after it — has so far been provable only from inside the
process. If the wire does not show the same order, then the browser is showing
something else and the guarantee has quietly become a property of the tests.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from forgepilot.providers.llm.replay import text_turn, tool_call_turn
from forgepilot.runtime.tools.pre_execute import PreDecision, PreDecisionKind
from forgepilot.seams.refs import SessionId


def ask_for(tool: str):
    """A pre-execute listener that escalates one tool to a human."""

    def listener(preview, scope):
        if preview.tool == tool:
            return PreDecision(kind=PreDecisionKind.ASK, reason="writes to the workspace")
        return None

    return listener


@dataclass
class Frame:
    event: str
    id: str | None
    data: dict


async def read_frames(response, sink: list[Frame]) -> None:
    """Parse the SSE wire format into frames until the connection ends."""
    event, ident = "message", None
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("id:"):
            ident = line[3:].strip()
        elif line.startswith("data:"):
            raw = line[5:].strip()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw}
            sink.append(Frame(event=event, id=ident, data=payload))
        elif not line:
            event, ident = "message", None


async def until(predicate, *, timeout: float = 10.0) -> None:
    """Wait for the stream to reach a state, or fail saying it never did."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the stream never reached the expected state in time")


def kinds(frames: list[Frame]) -> list[str]:
    return [f.event for f in frames]


def live_for(rig, session_id: str):
    return rig.registry.sessions[SessionId(session_id)]


async def new_session(rig) -> str:
    response = await rig.client.post("/sessions", headers=rig.auth())
    assert response.status_code == 201, response.text
    return response.json()["sessionId"]


def stream_of(rig, session_id: str, **extra_headers):
    """An open event stream, plus the frame list it is filling."""
    frames: list[Frame] = []
    return rig.client.stream(
        "GET",
        f"/sessions/{session_id}/events",
        headers={**rig.auth(), **extra_headers},
    ), frames


# ---------------------------------------------------------------------------
# The acceptance criterion
# ---------------------------------------------------------------------------
async def test_the_prompt_lands_on_the_stream_before_the_tool_runs(live_api) -> None:
    """The M4 property, seen from the client side.

    If this ever passes with the tool result ahead of the prompt, either the
    phase order changed or the stream stopped being the log — and both are worse
    than a failing test.
    """
    session_id = await new_session(live_api)
    live = live_for(live_api, session_id)
    live.runtime.pipeline.on_pre_execute(ask_for("write_file"))
    live_api.llm.script(
        tool_call_turn(
            "write_file",
            json.dumps({"path": "src/new.py", "content": "print('hi')\n"}),
            call_id="c1",
        ),
        text_turn("done"),
    )

    context, frames = stream_of(live_api, session_id)
    async with context as response:
        reader = asyncio.create_task(read_frames(response, frames))
        try:
            posted = await live_api.client.post(
                f"/sessions/{session_id}/messages",
                json={"text": "add a file"},
                headers=live_api.auth(),
            )
            assert posted.status_code == 202

            await until(lambda: "approval/asked" in kinds(frames))

            # The turn is now blocked on a human. Nothing may have run yet.
            assert "tool/result" not in kinds(frames), (
                "the tool body produced a result before anyone approved it"
            )
            assert not (live_api.workspace / "src" / "new.py").exists(), (
                "the file was written before the approval was answered"
            )

            call_id = next(f.data["data"]["callId"] for f in frames if f.event == "approval/asked")
            answered = await live_api.client.post(
                f"/approvals/{call_id}-approval",
                json={"decision": "allowed-once"},
                headers=live_api.auth(),
            )
            assert answered.status_code == 200
            assert answered.json()["answered"] is True

            await until(lambda: "turn/end" in kinds(frames))
        finally:
            reader.cancel()

    order = kinds(frames)
    assert order.index("approval/asked") < order.index("tool/result")
    assert (live_api.workspace / "src" / "new.py").exists(), "the tool never actually ran"


async def test_declining_leaves_the_workspace_untouched(live_api) -> None:
    """A denial is not a formality: the effect must not have happened."""
    session_id = await new_session(live_api)
    live = live_for(live_api, session_id)
    live.runtime.pipeline.on_pre_execute(ask_for("write_file"))
    live_api.llm.script(
        tool_call_turn(
            "write_file", json.dumps({"path": "src/no.py", "content": "x"}), call_id="c9"
        ),
        text_turn("understood"),
    )

    context, frames = stream_of(live_api, session_id)
    async with context as response:
        reader = asyncio.create_task(read_frames(response, frames))
        try:
            await live_api.client.post(
                f"/sessions/{session_id}/messages",
                json={"text": "go"},
                headers=live_api.auth(),
            )
            await until(lambda: "approval/asked" in kinds(frames))
            await live_api.client.post(
                "/approvals/c9-approval",
                json={"decision": "rejected"},
                headers=live_api.auth(),
            )
            await until(lambda: "turn/end" in kinds(frames))
        finally:
            reader.cancel()

    assert not (live_api.workspace / "src" / "no.py").exists()
    # And the model is told, or it will simply ask again.
    assert "tool/result" in kinds(frames)


async def test_an_unanswered_prompt_becomes_a_refusal(live_api) -> None:
    """Fail-closed, over HTTP.

    The timeout is not a policy of its own — it exists so that a human closing
    the tab produces the same outcome as saying no, rather than a turn pinned
    open forever.
    """
    session_id = await new_session(live_api)
    live = live_for(live_api, session_id)
    live.runtime.pipeline.on_pre_execute(ask_for("write_file"))
    live_api.llm.script(
        tool_call_turn(
            "write_file", json.dumps({"path": "src/never.py", "content": "x"}), call_id="c7"
        ),
        text_turn("understood"),
    )

    context, frames = stream_of(live_api, session_id)
    async with context as response:
        reader = asyncio.create_task(read_frames(response, frames))
        try:
            await live_api.client.post(
                f"/sessions/{session_id}/messages",
                json={"text": "go"},
                headers=live_api.auth(),
            )
            await until(lambda: "turn/end" in kinds(frames), timeout=20.0)
        finally:
            reader.cancel()

    assert not (live_api.workspace / "src" / "never.py").exists()
    decided = [f for f in frames if f.event == "approval/decided"]
    assert decided and decided[-1].data["data"]["outcome"] == "unavailable"


# ---------------------------------------------------------------------------
# The stream as a stream
# ---------------------------------------------------------------------------
async def test_a_second_turn_on_a_running_session_is_refused(live_api) -> None:
    """One session, one runner — overlapping turns would interleave the log."""
    session_id = await new_session(live_api)
    live = live_for(live_api, session_id)
    live.runtime.pipeline.on_pre_execute(ask_for("write_file"))
    live_api.llm.script(
        tool_call_turn(
            "write_file", json.dumps({"path": "src/x.py", "content": "x"}), call_id="c1"
        ),
        text_turn("ok"),
    )

    context, frames = stream_of(live_api, session_id)
    async with context as response:
        reader = asyncio.create_task(read_frames(response, frames))
        try:
            first = await live_api.client.post(
                f"/sessions/{session_id}/messages",
                json={"text": "one"},
                headers=live_api.auth(),
            )
            assert first.status_code == 202
            await until(lambda: "approval/asked" in kinds(frames))

            second = await live_api.client.post(
                f"/sessions/{session_id}/messages",
                json={"text": "two"},
                headers=live_api.auth(),
            )
            assert second.status_code == 409

            await live_api.client.post(
                "/approvals/c1-approval",
                json={"decision": "rejected"},
                headers=live_api.auth(),
            )
            await until(lambda: "turn/end" in kinds(frames))
        finally:
            reader.cancel()


async def test_resuming_from_a_sequence_number_replays_without_gaps_or_repeats(
    live_api,
) -> None:
    """``Last-Event-ID`` is the log's ``seq``, so a reconnect is a read."""
    session_id = await new_session(live_api)
    live_api.llm.script(text_turn("hello"), text_turn("again"))

    context, first = stream_of(live_api, session_id)
    async with context as response:
        reader = asyncio.create_task(read_frames(response, first))
        try:
            await live_api.client.post(
                f"/sessions/{session_id}/messages",
                json={"text": "hi"},
                headers=live_api.auth(),
            )
            await until(lambda: "turn/end" in kinds(first))
        finally:
            reader.cancel()

    seqs = [int(f.id) for f in first if f.id is not None]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), "duplicate or unordered"

    # Reconnect from the middle, the way a browser would after a dropped socket.
    cut = seqs[len(seqs) // 2]
    context, second = stream_of(live_api, session_id, **{"Last-Event-ID": str(cut)})
    expected = [s for s in seqs if s > cut]
    async with context as response:
        reader = asyncio.create_task(read_frames(response, second))
        try:
            await until(lambda: len(second) >= len(expected))
        finally:
            reader.cancel()

    resumed = [int(f.id) for f in second if f.id is not None]
    assert resumed[: len(expected)] == expected, "the replay did not continue exactly"


async def test_every_streamed_event_carries_its_sequence_number(live_api) -> None:
    """The durable channel is history, so every frame is citable by position."""
    session_id = await new_session(live_api)
    live_api.llm.script(text_turn("hi"))

    context, frames = stream_of(live_api, session_id)
    async with context as response:
        reader = asyncio.create_task(read_frames(response, frames))
        try:
            await live_api.client.post(
                f"/sessions/{session_id}/messages",
                json={"text": "hi"},
                headers=live_api.auth(),
            )
            await until(lambda: "turn/end" in kinds(frames))
        finally:
            reader.cancel()

    assert frames
    assert all(f.id is not None for f in frames), "a durable event went out without an id"


async def test_a_malformed_cursor_is_refused_rather_than_ignored(api, session_id) -> None:
    """Silently replaying everything would leave the client unaware its cursor
    was discarded."""
    response = await api.client.get(
        f"/sessions/{session_id}/events",
        headers={**api.auth(), "Last-Event-ID": "not-a-number"},
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# What survives a restart, and what must not
# ---------------------------------------------------------------------------
async def test_a_prompt_outlives_a_restart_but_its_answer_cannot(api, session_id) -> None:
    """The log is what survives; a pending question is not.

    This is the case the ``approval/asked`` flush exists for. Reopen the session
    from storage after the turn is gone and the prompt is still there — which is
    right, it really was asked. What must **not** happen is a client answering it
    and being told the answer landed somewhere.

    A UI that shows a stale prompt and a silent success is worse than one that
    shows nothing: the operator believes a tool call was approved when nothing is
    left to approve.
    """
    import asyncio

    from forgepilot.httpapi.registry import SessionRegistry
    from forgepilot.runtime.session import Session
    from forgepilot.seams.approval import ApprovalOutcome

    live = live_for(api, session_id)
    live.runtime.pipeline.on_pre_execute(ask_for("write_file"))
    api.llm.script(
        tool_call_turn(
            "write_file", json.dumps({"path": "src/restart.py", "content": "x"}), call_id="c5"
        ),
        text_turn("ok"),
    )

    await api.client.post(
        f"/sessions/{session_id}/messages", json={"text": "go"}, headers=api.auth()
    )
    await until(lambda: live.runtime.session.high_water > 0, timeout=5.0)
    await asyncio.sleep(0.2)

    # The process dies here. What a fresh server can see is only the log.
    restarted = SessionRegistry(root=api.root, approval_timeout=0.1)

    assert await restarted.answer("c5-approval", ApprovalOutcome.ALLOWED_ONCE) is False, (
        "a fresh process claimed to answer a question it never had"
    )

    reopened = await Session.open(api.root.sessionPersistence, SessionId(session_id))
    kinds = [e.kind for e in reopened.events]
    assert "approval/asked" in kinds, "the prompt was not durable when it was raised"
    # And recovery closed the turn the dead process left open, rather than
    # dropping history that really happened.
    assert kinds.count("turn/end") == 1
    assert reopened.events[-1].data.get("reason") == "interrupted"
