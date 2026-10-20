"""Projection and the *model-visible means logged* invariant.

The invariant only exists if violating it fails, so most of this file is about
the failure paths. A warning would be read past; the point of these tests is that
a request which the log cannot vouch for kills the step.
"""

from __future__ import annotations

import pytest

from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.kernel.errors import ModelVisibleNotLogged
from forgepilot.providers.llm.replay import ReplayLLMPlugin, text_turn
from forgepilot.runtime.log_guard import assert_logged, guarded_complete
from forgepilot.runtime.projection import build_request, derive_messages
from forgepilot.runtime.session import Session
from forgepilot.seams.llm import ChatMessage, ChatRequest, Role
from forgepilot.seams.refs import PrincipalId, TenantId, TurnId
from forgepilot.seams.security import ExecutionConstraints, Principal
from forgepilot.seams.session import EventKind, SurfaceOp


async def seeded(store, sid) -> Session:
    """A session with one complete turn of real content."""
    session = await Session.open(store, sid)
    await session.turn_start(TurnId(1))
    await session.user_message(TurnId(1), "why does login retry forever?")
    await session.assistant_message(
        TurnId(1),
        1,
        "let me look",
        tool_calls=[{"callId": "c1", "name": "code_search", "arguments": "{}"}],
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
    await session.append(EventKind.TASK_STATE, {"state": "running"}, surface=SurfaceOp.APPEND)
    await session.flush()

    with pytest.raises(ValueError, match="no rendering"):
        await build_request(session, model="m", system_sections=(), turn=TurnId(1), step=1)


# ---------------------------------------------------------------------------
# Where a REPLACE lands — the fold's ordering, which compaction depends on
# ---------------------------------------------------------------------------
def assert_no_orphan_tool_calls(messages) -> None:
    """Every assistant ``tool_calls`` block is answered immediately after it.

    The failure this catches surfaces **one turn later**, as a 400 from the
    endpoint, with a traceback pointing at the request builder rather than at
    whatever arranged the messages. Asserting on the finished message list is the
    only place the two are connected.
    """
    for index, message in enumerate(messages):
        if message.role is not Role.ASSISTANT or not message.tool_calls:
            continue
        wanted = [call.call_id for call in message.tool_calls]
        following = messages[index + 1 : index + 1 + len(wanted)]
        answered = [m.tool_call_id for m in following if m.role is Role.TOOL]
        assert answered == wanted, (
            f"assistant message {index} asked for {wanted} but is followed by "
            f"{[str(m.role) for m in following]} answering {answered}. "
            f"An unanswered tool call is rejected outright by every endpoint."
        )


async def two_tool_turns(store, sid) -> Session:
    """Two complete call/result pairs, so masking one can break adjacency."""
    session = await Session.open(store, sid)
    for turn, call in ((TurnId(1), "c1"), (TurnId(2), "c2")):
        await session.turn_start(turn)
        await session.assistant_message(
            turn,
            1,
            f"looking {call}",
            tool_calls=[{"callId": call, "name": "read_file", "arguments": "{}"}],
        )
        await session.tool_call(turn, 1, call_id=call, name="read_file", arguments_json="{}")
        await session.tool_result(turn, 1, call_id=call, content=f"{call}: a very large file body")
        await session.turn_end(turn, "completed")
    await session.flush()
    return session


async def test_masking_one_tool_result_keeps_it_next_to_its_call(created) -> None:
    """**The defect this section exists for, and it was measured.**

    A compaction masks an old tool result by appending a new event that supersedes
    it. The fold used to *remove* the superseded event and **append** the
    replacement at the end of the visible list — which moves the result away from
    the call it answers. Observed, on a two-pair conversation, masking only the
    first result:

        assistant calls=['c1']      <- its result is not next
        assistant calls=['c2']
        tool    result_for='c2'
        tool    result_for='c1'     <- three positions late

    `assistant(tool_calls=['c1'])` followed by another assistant message is an
    orphan tool call, and endpoints reject the whole request for it.
    """
    session = await two_tool_turns(*created)
    result_seq = next(
        e.seq
        for e in session.committed_events
        if e.kind == EventKind.TOOL_RESULT and e.data.get("callId") == "c1"
    )
    await session.append(
        EventKind.TOOL_RESULT,
        {"callId": "c1", "content": "[tool output elided]", "supersedes": [result_seq, result_seq]},
        surface=SurfaceOp.REPLACE,
    )
    await session.flush()

    messages = derive_messages(session.events)

    assert_no_orphan_tool_calls(messages)
    # And the masked content is the masked content, in the call's own place.
    tools = [m for m in messages if m.role is Role.TOOL]
    assert [m.content for m in tools] == ["[tool output elided]", "c2: a very large file body"]


async def test_a_summary_lands_where_the_span_was_not_at_the_end(created) -> None:
    """A replaced range is *replaced*, not moved.

    Appending the replacement means a summary of the first two turns appears
    **after** turn three — which reads as a history that happened in the wrong
    order. It also silently reorders anything the model is asked to reason about.
    """
    session = await two_tool_turns(*created)
    await session.append(
        EventKind.USER_MESSAGE,
        {"text": "[summary of turns 1-2]", "supersedes": [1, 8]},
        surface=SurfaceOp.REPLACE,
    )
    await session.flush()

    messages = derive_messages(session.events)

    assert messages[0].content == "[summary of turns 1-2]", (
        "the summary of the opening turns was appended rather than put in their place"
    )
    assert_no_orphan_tool_calls(messages)


async def test_a_summary_over_a_masked_region_absorbs_the_placeholders(created) -> None:
    """Two compaction passes, nested — the case the block rule is about.

    The first pass masks a tool result, so a placeholder sits at the *position* of
    an event whose **seq** is old while its own seq is new. A second pass that
    summarises a span covering that original must absorb the placeholder: its
    content belongs to the span being summarised.

    This is why the replacement takes the whole block from the first match to the
    last rather than only the events whose seqs match — the placeholder's seq is
    outside the range while its position is inside it, and leaving it behind would
    strand a message from a range that no longer exists.
    """
    session = await two_tool_turns(*created)
    first_result = next(
        e.seq
        for e in session.committed_events
        if e.kind == EventKind.TOOL_RESULT and e.data.get("callId") == "c1"
    )
    await session.append(
        EventKind.TOOL_RESULT,
        {
            "callId": "c1",
            "content": "[tool output elided]",
            "supersedes": [first_result, first_result],
        },
        surface=SurfaceOp.REPLACE,
    )
    await session.flush()

    # Second pass: summarise everything, which covers the original that
    # placeholder stands for.
    highest = max(e.seq for e in session.committed_events)
    await session.append(
        EventKind.USER_MESSAGE,
        {"text": "[summary]", "supersedes": [1, highest]},
        surface=SurfaceOp.REPLACE,
    )
    await session.flush()

    messages = derive_messages(session.events)

    assert [m.content for m in messages] == ["[summary]"], (
        "a placeholder from an earlier pass was stranded outside the summary that "
        "covers it — the model would see a fragment of a range that is gone"
    )
    assert_no_orphan_tool_calls(messages)
