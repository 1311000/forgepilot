"""`todo_write`: the plan is a tool result, so the log already has it.

The interesting thing about this tool is what it does *not* have: no store, no
side channel, no capability. The list it is handed back is the answer, so the
session log holds it by the same rule that holds every other tool result, and a
later turn sees it through the projection. A mutable todo store would have made
the plan a second account of the turn's progress — one the log could contradict.

The tests below are therefore about the *shape* of a plan: one thing in progress,
each item a line a reader can check, and a rendering that says how much is left.
"""

from __future__ import annotations

import json

from forgepilot.runtime.tools.builtin import (
    MAX_TODO_CONTENT,
    MAX_TODOS,
    build_tool_definitions,
)
from forgepilot.seams.refs import PrincipalId, SessionId, TenantId, TurnId
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal
from forgepilot.seams.tools import Capabilities, ToolCall, ToolContext, ToolEffect


class _NoSandbox:
    def probe(self):
        raise AssertionError("todo_write must not probe a sandbox")


def _definition():
    return next(d for d in build_tool_definitions(_NoSandbox()) if d.name == "todo_write")


def _context() -> ToolContext:
    return ToolContext(
        session_id=SessionId("s-todo"),
        constraints=ExecutionConstraints.for_principal(
            Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
            permitted_operations=frozenset({Operation.READ_CODE}),
            network=NetworkAccess.DENY,
        ),
        turn=TurnId("t-1"),
        step=1,
        capabilities=Capabilities(),
        workspace_root=None,
    )


async def _write(todos):
    return await _definition().execute(
        ToolCall(call_id="c-1", name="todo_write", arguments_json=json.dumps({"todos": todos})),
        _context(),
    )


async def test_the_list_is_the_answer_and_says_how_much_is_left() -> None:
    outcome = await _write(
        [
            {"content": "read the failing test", "status": "completed"},
            {"content": "fix the off-by-one", "status": "in_progress"},
            {"content": "run the suite", "status": "pending"},
        ]
    )

    assert outcome.is_error is False
    assert outcome.content.splitlines()[0] == (
        "todo list: 1 of 3 done, 2 open; working on: fix the off-by-one"
    )
    assert "[x] read the failing test" in outcome.content
    assert "[~] fix the off-by-one" in outcome.content
    assert "[ ] run the suite" in outcome.content
    assert outcome.meta["done"] == 1
    assert outcome.meta["open"] == 2


async def test_the_whole_list_comes_back_in_the_meta() -> None:
    outcome = await _write(
        [
            {"content": "  padded  ", "status": "pending"},
            {"content": "done", "status": "completed"},
        ]
    )

    assert outcome.meta["todos"] == [
        {"content": "padded", "status": "pending"},
        {"content": "done", "status": "completed"},
    ]


async def test_two_things_in_progress_is_refused() -> None:
    # Exactly one thing is being worked on; a plan that says otherwise is a plan
    # nobody is following.
    outcome = await _write(
        [
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ]
    )

    assert outcome.is_error is True
    assert "2 todos are in progress" in outcome.content
    assert "at most one" in outcome.content


async def test_a_list_with_nothing_in_progress_is_allowed() -> None:
    outcome = await _write([{"content": "a", "status": "completed"}])

    assert outcome.is_error is False


async def test_an_empty_list_is_refused() -> None:
    outcome = await _write([])

    assert outcome.is_error is True
    assert "non-empty" in outcome.content


async def test_a_missing_list_is_refused() -> None:
    outcome = await _definition().execute(
        ToolCall(call_id="c-1", name="todo_write", arguments_json="{}"),
        _context(),
    )

    assert outcome.is_error is True
    assert "non-empty list" in outcome.content


async def test_an_unknown_status_is_refused_by_name() -> None:
    outcome = await _write([{"content": "a", "status": "blocked"}])

    assert outcome.is_error is True
    assert "'blocked'" in outcome.content
    assert "pending, in_progress, completed" in outcome.content


async def test_blank_content_is_refused() -> None:
    outcome = await _write([{"content": "   ", "status": "pending"}])

    assert outcome.is_error is True
    assert "content" in outcome.content


async def test_a_paragraph_is_not_a_todo() -> None:
    outcome = await _write([{"content": "x" * (MAX_TODO_CONTENT + 1), "status": "pending"}])

    assert outcome.is_error is True
    assert str(MAX_TODO_CONTENT) in outcome.content


async def test_a_wish_list_is_refused() -> None:
    todos = [{"content": f"item {i}", "status": "pending"} for i in range(MAX_TODOS + 1)]

    outcome = await _write(todos)

    assert outcome.is_error is True
    assert str(MAX_TODOS) in outcome.content


async def test_a_non_object_todo_is_refused_with_its_index() -> None:
    outcome = await _write(["not an object"])

    assert outcome.is_error is True
    assert "todo 1" in outcome.content


def test_it_asks_for_no_capability() -> None:
    definition = _definition()

    assert definition.effects == frozenset({ToolEffect.SESSION})
    # No operation to check: the only thing this call writes is the record of
    # itself, and being in the tool set is the authority to do that.
    assert definition.required_operation is None
    # And no targets at all: there is no path, no repository and no command to
    # scope, so the guard is given nothing rather than an empty-looking path.
    assert definition.describe_targets is None
