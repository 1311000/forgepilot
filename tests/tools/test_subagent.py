"""``spawn_subagent``: a child turn whose authority is narrower by construction.

The interesting properties are about what the child *cannot* do and what the parent
can still read afterwards, so the tests assert the constraint set handed to the
child and the lineage recorded in its session header — not just that a string came
back.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from forgepilot.kernel import dispose_fiber
from forgepilot.providers.llm.replay import text_turn
from forgepilot.runtime.session import Session
from forgepilot.runtime.tools.subagent import SPAWN_TOOL, SubagentToolPlugin
from forgepilot.seams.refs import SessionId
from forgepilot.seams.security import NetworkAccess, Operation
from forgepilot.seams.session import EventKind, SessionHeader
from forgepilot.seams.tools import Capabilities, ToolCall, ToolContext, ToolEffect
from tests.conftest import constraints_for


def _call(**arguments: Any) -> ToolCall:
    return ToolCall(
        call_id="c-sub",
        name=SPAWN_TOOL,
        arguments_json=json.dumps(arguments or {"task": "where is the page size clamped?"}),
    )


def _ctx(rig, *operations: Operation) -> ToolContext:
    return ToolContext(
        session_id=SessionId(str(rig.session.header.session_id)),
        constraints=constraints_for(*(operations or (Operation.SPAWN_SUBAGENT,))),
        turn=1,
        step=1,
        capabilities=Capabilities(),
        workspace_root=rig.workspace,
    )


async def _definition(rig, **config: Any):
    fiber = await rig.root.plugin(SubagentToolPlugin, {"tool_names": ["read_file"], **config})
    return fiber, rig.registry.get(SPAWN_TOOL)


# ---------------------------------------------------------------------------
# The happy path, and what it leaves behind
# ---------------------------------------------------------------------------
async def test_the_child_runs_in_its_own_session_and_answers(loop_rig) -> None:
    fiber, definition = await _definition(loop_rig)
    try:
        loop_rig.llm.script(text_turn("the page size is clamped in limits.py:12"))

        outcome = await definition.execute(_call(), _ctx(loop_rig))
    finally:
        await dispose_fiber(fiber)

    assert not outcome.is_error, outcome.content
    assert "the page size is clamped in limits.py:12" in outcome.content
    child = SessionId(outcome.meta["child"])
    assert child != loop_rig.session.header.session_id

    header = await loop_rig.root.sessionPersistence.header(child)
    assert header.parent_session == loop_rig.session.header.session_id
    assert header.tenant == loop_rig.session.header.tenant

    child_session = await Session.open(loop_rig.root.sessionPersistence, child, recover=False)
    sent = " ".join(
        str(event.data.get("text", ""))
        for event in child_session.events
        if event.kind is EventKind.USER_MESSAGE
    )
    assert "page size clamped" in sent


async def test_the_child_gets_narrower_authority_than_its_parent(loop_rig) -> None:
    fiber, definition = await _definition(loop_rig, max_steps=4)
    captured: dict[str, Any] = {}

    async def fake_run_turn(**kwargs: Any):
        captured.update(kwargs)
        return SimpleNamespace(reason="completed", assistant_text="done", tool_calls=0)

    import forgepilot.runtime.loop.turn as turn_module

    saved = turn_module.run_turn
    turn_module.run_turn = fake_run_turn
    try:
        outcome = await definition.execute(
            _call(),
            _ctx(loop_rig, Operation.SPAWN_SUBAGENT, Operation.WRITE_CODE, Operation.RUN_TESTS),
        )
    finally:
        turn_module.run_turn = saved
        await dispose_fiber(fiber)

    assert not outcome.is_error, outcome.content
    child = captured["constraints"]
    assert Operation.SPAWN_SUBAGENT not in child.permitted_operations
    assert Operation.WRITE_CODE not in child.permitted_operations
    assert Operation.RUN_TESTS not in child.permitted_operations
    assert Operation.OPEN_PR not in child.permitted_operations
    assert Operation.FETCH_URL not in child.permitted_operations
    assert child.writable_paths == ()
    assert child.network is NetworkAccess.DENY
    assert captured["max_steps"] == 4
    names = {getattr(schema, "name", "") for schema in captured["tool_schemas"]}
    assert SPAWN_TOOL not in names
    assert "read_file" in names


async def test_a_subagent_cannot_spawn_a_subagent(loop_rig) -> None:
    fiber, definition = await _definition(loop_rig)
    store = loop_rig.root.sessionPersistence
    child = SessionId("sub-already-a-child")
    await store.create(
        SessionHeader(
            session_id=child,
            created_at=datetime.now(UTC),
            tenant=loop_rig.session.header.tenant,
            principal=loop_rig.session.header.principal,
            parent_session=loop_rig.session.header.session_id,
        )
    )
    ctx = ToolContext(
        session_id=child,
        constraints=constraints_for(Operation.SPAWN_SUBAGENT),
        turn=1,
        step=1,
        capabilities=Capabilities(),
        workspace_root=loop_rig.workspace,
    )
    try:
        outcome = await definition.execute(_call(), ctx)
    finally:
        await dispose_fiber(fiber)

    assert outcome.is_error
    assert "cannot spawn another subagent" in outcome.content


# ---------------------------------------------------------------------------
# Refusals that do not depend on the model
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("arguments_json", ["{}", '{"task": "   "}', '{"context": "x"}'])
async def test_a_missing_task_is_refused_before_anything_is_created(
    loop_rig, arguments_json
) -> None:
    fiber, definition = await _definition(loop_rig)
    call = ToolCall(call_id="c-empty", name=SPAWN_TOOL, arguments_json=arguments_json)
    try:
        outcome = await definition.execute(call, _ctx(loop_rig))
    finally:
        await dispose_fiber(fiber)

    assert outcome.is_error
    assert "`task` is required" in outcome.content


async def test_a_timeout_names_the_child_it_abandoned(loop_rig) -> None:
    fiber, definition = await _definition(loop_rig, timeout_s=5)

    async def never_returns(**_: Any):
        await asyncio.sleep(30)

    import forgepilot.runtime.loop.turn as turn_module

    saved = turn_module.run_turn
    turn_module.run_turn = never_returns
    try:
        outcome = await definition.execute(_call(), _ctx(loop_rig))
    finally:
        turn_module.run_turn = saved
        await dispose_fiber(fiber)

    assert outcome.is_error
    assert "did not finish" in outcome.content
    assert outcome.meta["reason"] == "timeout"
    assert outcome.meta["child"].startswith("sub-")


async def test_a_long_answer_is_truncated_and_points_at_the_child(loop_rig) -> None:
    fiber, definition = await _definition(loop_rig, max_chars=200)
    try:
        loop_rig.llm.script(text_turn("x" * 500))
        outcome = await definition.execute(_call(), _ctx(loop_rig))
    finally:
        await dispose_fiber(fiber)

    assert not outcome.is_error
    assert "[truncated" in outcome.content
    assert outcome.meta["truncated"] is True
    assert outcome.meta["child"] in outcome.content


def test_the_definition_says_which_effect_and_permission_it_needs() -> None:
    plugin = SubagentToolPlugin(SimpleNamespace(), {})

    definition = plugin.definition()

    assert definition.effects == frozenset({ToolEffect.SESSION})
    assert definition.required_operation is Operation.SPAWN_SUBAGENT
    assert definition.parameters["required"] == ["task"]
    assert "read-only subagent" in definition.description
