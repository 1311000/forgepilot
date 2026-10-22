"""Two replicas, one state directory, one log.

The single-process facts this file replaces were all in one place: the approval
future, the "is a turn running" question, and the right to start a turn. Each is
answered here by the shared coordinator, and each answer is asserted from the
*other* replica's point of view — because a replica proving its own memory works
proves nothing about the deployment.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from forgepilot.httpapi.registry import SessionOwnedElsewhere, SessionRegistry
from forgepilot.providers.coordination.files import FileCoordination
from forgepilot.providers.llm.replay import text_turn, tool_call_turn
from forgepilot.runtime.tools.pre_execute import PreDecision, PreDecisionKind
from forgepilot.seams.approval import ApprovalOutcome
from forgepilot.seams.refs import SessionId


def ask_for(tool: str):
    def listener(preview, scope):
        if preview.tool == tool:
            return PreDecision(kind=PreDecisionKind.ASK, reason="writes to the workspace")
        return None

    return listener


async def until(predicate, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the expected state never arrived in time")


def _kinds(live) -> list[str]:
    return [event.kind for event in live.runtime.session.events]


@pytest.mark.asyncio
async def test_an_approval_answered_on_one_replica_releases_the_other(api, tmp_path) -> None:
    shared = FileCoordination(root=tmp_path / "coord", poll_s=0.01)
    api.registry.coordinator = shared
    api.registry.holder = "replica-a"
    other = SessionRegistry(
        root=api.root, approval_timeout=5.0, coordinator=shared, holder="replica-b"
    )

    created = await api.client.post("/sessions", headers=api.auth())
    session_id = SessionId(created.json()["sessionId"])
    live = api.registry.sessions[session_id]
    live.runtime.pipeline.on_pre_execute(ask_for("write_file"))
    api.llm.script(
        tool_call_turn(
            "write_file", json.dumps({"path": "src/two.py", "content": "x"}), call_id="c9"
        ),
        text_turn("done"),
    )

    started = await api.client.post(
        f"/sessions/{session_id}/messages", json={"text": "go"}, headers=api.auth()
    )
    assert started.status_code == 202
    await until(lambda: "approval/asked" in _kinds(live))

    # Replica B has never run this session, and still knows three things: the
    # question exists, whose it is, and that a turn is in flight.
    pending = await other.lookup("c9-approval")
    assert pending is not None
    assert str(pending.session_id) == str(session_id)
    assert await other.active(session_id) is True
    assert [r.request_id for r in await other.pending_approvals(session_id)] == ["c9-approval"]

    # A second turn on the same session is refused *by the lease*, not by B's
    # empty memory of it: B builds its own handle for the session (what a real
    # replica does after reopening the log) and still cannot run it.
    from forgepilot.httpapi.registry import LiveSession

    mirror = LiveSession(runtime=live.runtime, principal=live.principal)
    with pytest.raises(SessionOwnedElsewhere):
        await other.start_turn(mirror, lambda: asyncio.sleep(0))

    assert await other.answer("c9-approval", ApprovalOutcome.ALLOWED_ONCE, principal="alice")
    await until(lambda: "tool/result" in _kinds(live))
    assert not await other.answer("c9-approval", ApprovalOutcome.REJECTED, principal="mallory"), (
        "a second answer must not land"
    )


@pytest.mark.asyncio
async def test_a_rejected_approval_from_the_other_replica_stops_the_tool(api, tmp_path) -> None:
    shared = FileCoordination(root=tmp_path / "coord", poll_s=0.01)
    api.registry.coordinator = shared
    api.registry.holder = "replica-a"
    other = SessionRegistry(root=api.root, approval_timeout=5.0, coordinator=shared, holder="b")

    created = await api.client.post("/sessions", headers=api.auth())
    session_id = SessionId(created.json()["sessionId"])
    live = api.registry.sessions[session_id]
    live.runtime.pipeline.on_pre_execute(ask_for("write_file"))
    api.llm.script(
        tool_call_turn(
            "write_file", json.dumps({"path": "src/denied.py", "content": "x"}), call_id="c10"
        ),
        text_turn("understood"),
    )

    await api.client.post(
        f"/sessions/{session_id}/messages", json={"text": "go"}, headers=api.auth()
    )
    await until(lambda: "approval/asked" in _kinds(live))
    assert await other.answer("c10-approval", ApprovalOutcome.REJECTED, principal="alice")
    await until(lambda: "tool/result" in _kinds(live))

    results = [e for e in live.runtime.session.events if e.kind == "tool/result"]
    assert results and results[-1].data.get("isError") is True, results
    # `tests/httpapi/conftest.py` boots the app with `workspace_root=tmp_path/"ws"`.
    assert not (tmp_path / "ws" / "src" / "denied.py").exists()
