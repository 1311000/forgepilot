"""Projection and the *model-visible means logged* invariant.

The invariant only exists if violating it fails, so most of this file is about
the failure paths. A warning would be read past; the point of these tests is that
a request which the log cannot vouch for kills the step.
"""

from __future__ import annotations

import pytest

from forgepilot.kernel.errors import ModelVisibleNotLogged
from forgepilot.providers.llm.replay import ReplayLLMPlugin, text_turn
from forgepilot.runtime.log_guard import assert_logged, guarded_complete, guarded_stream
from forgepilot.runtime.projection import build_request, derive_messages
from forgepilot.runtime.session import Session
from forgepilot.seams.llm import ChatMessage, ChatRequest, Role
from forgepilot.seams.refs import PrincipalId, TenantId, TurnId
from forgepilot.seams.security import ExecutionConstraints, Principal
from forgepilot.seams.session import EventKind, SessionEvent, SurfaceOp

from forgepilot.kernel import create_root, dispose_fiber


async def seeded(store, sid) -> Session:
    """A session with one complete turn of real content."""
    session = await Session.open(store, sid)
    await session.turn_start(TurnId(1))
    await session.user_message(TurnId(1), "why does login retry forever?")
    await session.assistant_message(
        TurnId(1), 1, "let me look", tool_calls=[{"callId": "c1", "name": "code_search", "arguments": "{}"}]
    )
    await session.tool_call(TurnId(1), 1, call_id="c1", name="code_search", arguments_json="{}")
    await session.tool_result(TurnId(1), 1, call_id="c1", content="src/login.py:41 retry_forever()")
    await session.turn_end(TurnId(1), "completed")
    await session.flush()
    return session


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------
async def test_projection_renders_only_surface_events(created) -> None:
    """Log-only events are durable but must never reach the model.

    The constraint snapshot is the interesting one: the model is never told its
    own allow-list, because a prompt is not a permission policy.
    """
    store, sid = created
    session = await seeded(store, sid)
    await session.constraint_snapshot(
        ExecutionConstraints.for_principal(
            Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
            allowed_repo_prefixes=frozenset({"git.corp/secret/*"}),
        )
    )
    await session.append(EventKind.APPROVAL_ASKED, {"tool": "shell"})  # log-only
    await session.flush()

    messages = derive_messages(session.events)

    assert [m.role for m in messages] == [Role.USER, Role.ASSISTANT, Role.TOOL]
    joined = " ".join(m.content or "" for m in messages)
    assert "git.corp/secret" not in joined, "the allow-list leaked into the prompt"
    assert "shell" not in joined, "log-only content leaked into the model"


async def test_every_message_carries_its_source_event(created) -> None:
    """This is what makes 'model-visible means logged' checkable at all."""
    store, sid = created
    session = await seeded(store, sid)

    messages = derive_messages(session.events)

    assert all(m.source_event_seq is not None for m in messages)
    logged = {e.seq for e in session.committed_events}
    assert all(m.source_event_seq in logged for m in messages)


async def test_tool_call_arguments_are_preserved_unparsed(created) -> None:
    """A malformed-argument failure must stay replayable."""
    store, sid = created
    session = await Session.open(store, sid)
    await session.turn_start(TurnId(1))
    await session.assistant_message(
        TurnId(1), 1, "", tool_calls=[{"callId": "c1", "name": "t", "arguments": "{not json"}]
    )
    await session.flush()

    messages = derive_messages(session.events)

    assistant = next(m for m in messages if m.role is Role.ASSISTANT)
    assert assistant.tool_calls[0].arguments_json == "{not json"


async def test_a_replace_event_supersedes_the_range_it_names(created) -> None:
    """Compaction summarises history without the log ever being edited."""
    store, sid = created
    session = await Session.open(store, sid)
    await session.user_message(TurnId(1), "long discussion one")
    await session.user_message(TurnId(1), "long discussion two")
    await session.append(
        EventKind.USER_MESSAGE,
        {"text": "[summary of the above]", "supersedes": [1, 2]},
        surface=SurfaceOp.REPLACE,
    )
    await session.flush()

    messages = derive_messages(session.events)

    assert [m.content for m in messages] == ["[summary of the above]"]
    assert messages[0].source_event_seq == 3


# ---------------------------------------------------------------------------
# build_request
# ---------------------------------------------------------------------------
async def test_build_request_records_a_log_only_header_first(created) -> None:
    store, sid = created
    session = await seeded(store, sid)

    request = await build_request(
        session, model="replay-1", system_sections=("be brief",), turn=TurnId(2), step=1
    )

    headers = [e for e in session.events if e.kind == EventKind.REQUEST_HEADER]
    assert len(headers) == 1
    assert headers[0].surface is SurfaceOp.LOG_ONLY, "the model does not see the header"
    assert headers[0].data["system"] == ["be brief"]
    assert request.system == ("be brief",)


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
async def test_a_message_without_provenance_is_rejected(created) -> None:
    store, sid = created
    session = await seeded(store, sid)

    request = ChatRequest(
        model="m",
        system=(),
        messages=(ChatMessage(role=Role.USER, content="smuggled in"),),
    )

    with pytest.raises(ModelVisibleNotLogged, match="no source_event_seq"):
        assert_logged(session, request)


async def test_a_message_citing_a_future_event_is_rejected(created) -> None:
    store, sid = created
    session = await seeded(store, sid)

    request = ChatRequest(
        model="m",
        system=(),
        messages=(ChatMessage(role=Role.USER, content="x", source_event_seq=9999),),
    )

    with pytest.raises(ModelVisibleNotLogged, match="only .* durable"):
        assert_logged(session, request)


async def test_unflushed_events_block_dispatch(created) -> None:
    """Sending now would risk the model being told something the log lacks."""
    store, sid = created
    session = await seeded(store, sid)
    await session.user_message(TurnId(2), "buffered, not written")

    request = ChatRequest(
        model="m",
        system=(),
        messages=derive_messages(session.committed_events),
    )

    with pytest.raises(ModelVisibleNotLogged, match="buffered"):
        assert_logged(session, request)


async def test_guarded_stream_flushes_so_the_caller_cannot_forget(created) -> None:
    """The flush belongs to the guard, not to a rule someone must remember."""
    store, sid = created
    session = await Session.open(store, sid)
    await session.turn_start(TurnId(1))
    await session.user_message(TurnId(1), "question")

    root = create_root()
    fiber = await root.plugin(ReplayLLMPlugin, {"model": "replay-1"})
    try:
        llm = root.llm
        llm.script(text_turn("answer"))
        request = await build_request(
            session, model="replay-1", system_sections=(), turn=TurnId(1), step=1
        )

        # Deliberately *not* flushed by the test: guarded_stream must do it.
        assert session.has_pending
        response = await guarded_complete(session, llm, request)

        assert response.message.content == "answer"
        assert not session.has_pending, "the guard flushed on the way through"
        assert await store.high_water(sid) > 0
    finally:
        await dispose_fiber(fiber)


async def test_a_surface_event_with_no_renderer_is_refused(created) -> None:
    """Better to fail than to show the model less than the log claims it saw."""
    store, sid = created
    session = await Session.open(store, sid)
    await session.append(
        EventKind.TASK_STATE, {"state": "running"}, surface=SurfaceOp.APPEND
    )
    await session.flush()

    with pytest.raises(ValueError, match="no rendering"):
        await build_request(
            session, model="m", system_sections=(), turn=TurnId(1), step=1
        )
