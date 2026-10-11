"""The agent loop: turns, steps, and the guarantees the log has to hold.

Every test here runs against a **scripted** model. A live endpoint would make
these slow, non-deterministic and credential-dependent, and — worse — the model's
tolerance would hide prompt-assembly bugs that a scripted model exposes
immediately. No test in this suite touches a network.

The two assertions that matter most are about what the *next* request contains:
that the model sees the tool results, and that a refused tool still enters
history. A loop that ran tools but never fed them back would look like it worked.
"""

from __future__ import annotations

import json

import pytest

from forgepilot.providers.llm.replay import text_turn, tool_call_turn
from forgepilot.runtime.loop.agent_loop import (
    AgentFactoryRegistry,
    ForgePilotAgentFactory,
    ForgePilotAgentLoop,
)
from forgepilot.runtime.loop.step import ContextWindowExceeded, accumulate
from forgepilot.runtime.loop.turn import run_turn
from forgepilot.seams.agent_loop import AgentSpec, UserInput
from forgepilot.seams.llm import Role, StreamChunk, Usage
from forgepilot.seams.refs import AgentId, SessionId
from forgepilot.seams.session import EventKind


def constraints_for(workspace, *, writable=("src",), operations=None):
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal

    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        permitted_operations=frozenset(operations or tuple(Operation)),
        writable_paths=tuple(writable),
        network=NetworkAccess.DENY,
    )


def spec_for(constraints, *, system: tuple[str, ...] = (), mode: str = "chat") -> AgentSpec:
    from forgepilot.seams.refs import Mode

    return AgentSpec(
        agent_id=AgentId("agent-1"),
        session_id=SessionId("loop-session"),
        mode=Mode(mode),
        constraints=constraints,
        system_sections=system,
    )


async def drive(rig, text: str = "why does login retry forever?", **kwargs):
    return await run_turn(
        session=rig.session,
        user_input=UserInput(text=text),
        llm=rig.llm,
        tools=rig.registry,
        constraints=kwargs.pop("constraints", constraints_for(rig.workspace)),
        model="replay-1",
        system_sections=("you are an enterprise coding assistant",),
        workspace_root=rig.workspace,
        tool_schemas=rig.registry.schemas(),
        tracker=rig.tracker,
        **kwargs,
    )


def events(rig):
    return rig.session.committed_events


def kinds(rig):
    return [e.kind for e in events(rig)]


# ---------------------------------------------------------------------------
# A plain turn
# ---------------------------------------------------------------------------
async def test_a_single_step_turn_produces_a_balanced_log(loop_rig) -> None:
    loop_rig.llm.script(text_turn("it retries because the cap is missing"))

    result = await drive(loop_rig)

    assert result.reason == "completed"
    assert "cap is missing" in result.assistant_text
    assert result.tool_calls == 0
    assert kinds(loop_rig) == [
        EventKind.TURN_START,
        EventKind.USER_MESSAGE,
        EventKind.STEP_START,
        EventKind.REQUEST_HEADER,
        EventKind.ASSISTANT_MESSAGE,
        EventKind.STEP_END,
        EventKind.TURN_END,
    ]


async def test_assistant_message_carries_usage(loop_rig) -> None:
    """Usage is recorded, not just consumed — cost accounting reads the log."""
    loop_rig.llm.script(
        text_turn("done", usage=Usage(input_tokens=120, output_tokens=8))
    )

    await drive(loop_rig)

    assistant = next(e for e in events(loop_rig) if e.kind == EventKind.ASSISTANT_MESSAGE)
    assert assistant.data["usage"]["inputTokens"] == 120
    assert assistant.data["usage"]["outputTokens"] == 8
    assert loop_rig.tracker.tokens == 128


# ---------------------------------------------------------------------------
# The tool round trip
# ---------------------------------------------------------------------------
async def test_a_tool_call_is_logged_before_its_result(loop_rig) -> None:
    """Durable-before-execution, asserted on the log rather than trusted."""
    loop_rig.llm.script(
        tool_call_turn("read_file", json.dumps({"path": "src/a.py"})),
        text_turn("the file defines retry"),
    )

    await drive(loop_rig)

    order = kinds(loop_rig)
    call_at = order.index(EventKind.TOOL_CALL)
    result_at = order.index(EventKind.TOOL_RESULT)
    assert call_at < result_at


async def test_the_model_sees_the_tool_result_on_the_next_step(loop_rig) -> None:
    """The assertion a loop that ran tools but never fed them back would fail."""
    loop_rig.llm.script(
        tool_call_turn("read_file", json.dumps({"path": "src/a.py"})),
        text_turn("the file defines retry"),
    )

    await drive(loop_rig)

    assert len(loop_rig.llm.requests) == 2, "the second request is the one under test"
    second = loop_rig.llm.requests[1]
    tool_messages = [m for m in second.messages if m.role is Role.TOOL]

    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call-1"
    assert "def retry()" in (tool_messages[0].content or "")
    assert tool_messages[0].source_event_seq is not None

    # And the model's own request is present, or the message list is malformed.
    assistant = [m for m in second.messages if m.role is Role.ASSISTANT]
    assert assistant and assistant[0].tool_calls[0].name == "read_file"


async def test_a_refused_tool_still_enters_history(loop_rig) -> None:
    """A refusal the model cannot see is a refusal it will ask for again."""
    loop_rig.llm.script(
        tool_call_turn("write_file", json.dumps({"path": "../escape", "content": "x"})),
        text_turn("understood, I will stay in the workspace"),
    )
    read_only = constraints_for(
        loop_rig.workspace, writable=()  # nothing is writable
    )

    await drive(loop_rig, constraints=read_only)

    result_event = next(e for e in events(loop_rig) if e.kind == EventKind.TOOL_RESULT)
    assert result_event.data["isError"] is True

    second = loop_rig.llm.requests[1]
    tool_messages = [m for m in second.messages if m.role is Role.TOOL]
    assert tool_messages, "the refusal must reach the model"
    assert "writable" in tool_messages[0].content or "refused" in tool_messages[0].content.lower()


async def test_a_turn_with_several_steps_closes_once(loop_rig) -> None:
    loop_rig.llm.script(
        tool_call_turn("read_file", json.dumps({"path": "src/a.py"}), call_id="c1"),
        tool_call_turn("read_file", json.dumps({"path": "src/a.py"}), call_id="c2"),
        text_turn("done"),
    )

    result = await drive(loop_rig)

    assert result.tool_calls == 2
    assert kinds(loop_rig).count(EventKind.TURN_START) == 1
    assert kinds(loop_rig).count(EventKind.TURN_END) == 1
    assert kinds(loop_rig).count(EventKind.STEP_START) == 3


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------
async def test_budget_exhaustion_closes_the_turn_with_a_reason(loop_rig) -> None:
    loop_rig.tracker.tool_calls = 10_000  # already over

    result = await drive(loop_rig)

    assert result.reason == "budget"
    assert kinds(loop_rig)[-1] == EventKind.TURN_END
    assert events(loop_rig)[-1].data["reason"] == "budget"


async def test_the_step_ceiling_is_the_number_of_steps(loop_rig) -> None:
    """⚠️ **`max_steps` used to mean `max_steps - 1` steps.**

    The ceiling was checked *inside* the loop as `if step == max_steps - 1: break`,
    one iteration early, so the last permitted step never ran: a ceiling of 4
    produced 3, and `DEFAULT_MAX_STEPS = 12` meant 11.

    This asserted `<= max_steps`, which passes for either number — the same shape
    as the sandbox test that asserted a `--` was present because the test and the
    code shared the wrong assumption. Counted exactly now.
    """
    for index in range(20):
        loop_rig.llm.script(
            tool_call_turn("read_file", json.dumps({"path": "src/a.py"}), call_id=f"c{index}")
        )

    result = await drive(loop_rig, max_steps=3)

    assert result.reason == "error", "the ceiling ends the turn with a reason of its own"
    assert kinds(loop_rig).count(EventKind.STEP_START) == 3
    assert kinds(loop_rig)[-1] == EventKind.TURN_END


async def test_context_window_exceeded_refuses_and_still_closes_the_turn(loop_rig) -> None:
    """Refused rather than truncated — and the log stays balanced either way."""

    class TinyWindowLLM:
        model = "tiny"

        def __init__(self, inner):
            self._inner = inner

        def capabilities(self):
            caps = self._inner.capabilities()
            return type(caps)(
                tool_calling=True,
                streaming=True,
                max_context_tokens=1,  # nothing fits
                supports_system_prompt=True,
            )

        def stream(self, request):
            return self._inner.stream(request)

        async def complete(self, request):
            return await self._inner.complete(request)

    loop_rig.llm.script(text_turn("never reached"))

    with pytest.raises(ContextWindowExceeded):
        await run_turn(
            session=loop_rig.session,
            user_input=UserInput(text="a question long enough to exceed one token"),
            llm=TinyWindowLLM(loop_rig.llm),
            tools=loop_rig.registry,
            constraints=constraints_for(loop_rig.workspace),
            model="tiny",
            workspace_root=loop_rig.workspace,
        )

    assert kinds(loop_rig)[-1] == EventKind.TURN_END, "the turn must not be left open"
    assert events(loop_rig)[-1].data["reason"] == "error"


async def test_a_cancelled_turn_stops_at_a_step_boundary(loop_rig) -> None:
    """Cooperative cancellation, so the log never has a call without a result."""
    cancelled = {"yes": False}

    loop_rig.llm.script(
        tool_call_turn("read_file", json.dumps({"path": "src/a.py"})),
        text_turn("never reached"),
    )

    def is_cancelled() -> bool:
        cancelled["yes"] = True
        return cancelled["yes"]

    result = await drive(loop_rig, is_cancelled=lambda: False)
    assert result.reason == "completed"

    # Now with cancellation armed from the start.
    loop_rig.llm.script(text_turn("unused"))
    result = await drive(loop_rig, is_cancelled=lambda: True)
    assert result.reason == "cancelled"
    assert events(loop_rig)[-1].data["reason"] == "cancelled"


# ---------------------------------------------------------------------------
# Delta accumulation
# ---------------------------------------------------------------------------
def test_accumulate_reassembles_interleaved_tool_call_fragments() -> None:
    """Fragments are keyed by index, not appended in arrival order.

    An endpoint may interleave the argument fragments of two calls. Concatenating
    in arrival order would splice one call's arguments into the other's — and the
    result would look like valid JSON often enough to be missed.
    """
    chunks = [
        StreamChunk(kind="tool_call_delta", tool_index=0, tool_call_id="a", tool_name="first", arguments_delta='{"x":'),
        StreamChunk(kind="tool_call_delta", tool_index=1, tool_call_id="b", tool_name="second", arguments_delta='{"y":'),
        StreamChunk(kind="tool_call_delta", tool_index=0, arguments_delta="1}"),
        StreamChunk(kind="tool_call_delta", tool_index=1, arguments_delta="2}"),
        StreamChunk(kind="usage", usage=Usage(input_tokens=3, output_tokens=4)),
    ]

    result = accumulate(chunks)

    assert [(c.name, c.arguments_json) for c in result.tool_calls] == [
        ("first", '{"x":1}'),
        ("second", '{"y":2}'),
    ]
    assert result.usage.input_tokens == 3


def test_accumulate_ignores_a_tool_delta_with_no_name() -> None:
    """A fragment arriving for an index that never carried a name is not a call."""
    result = accumulate([StreamChunk(kind="tool_call_delta", tool_index=0, arguments_delta="{")])

    assert result.tool_calls == ()


# ---------------------------------------------------------------------------
# The engine is swappable
# ---------------------------------------------------------------------------
async def test_the_factory_registry_selects_an_engine_by_key(loop_rig) -> None:
    registry = AgentFactoryRegistry(loop_rig.root)

    class OtherEngine:
        key = "other"

        def build(self, spec, kernel_ctx):
            return ForgePilotAgentLoop(kernel_ctx=kernel_ctx)

    registry.register_factory(ForgePilotAgentFactory())
    registry.register_factory(OtherEngine())

    assert registry.keys() == ("forgepilot", "other")
    assert isinstance(registry.build("forgepilot", spec_for(constraints_for(loop_rig.workspace)), loop_rig.root), ForgePilotAgentLoop)
    assert isinstance(registry.build("other", spec_for(constraints_for(loop_rig.workspace)), loop_rig.root), ForgePilotAgentLoop)


async def test_an_unknown_engine_key_is_refused_with_the_alternatives(loop_rig) -> None:
    from forgepilot.kernel.errors import PluginContractViolation

    registry = AgentFactoryRegistry(loop_rig.root)
    registry.register_factory(ForgePilotAgentFactory())

    with pytest.raises(PluginContractViolation, match="forgepilot"):
        registry.build("pi", spec_for(constraints_for(loop_rig.workspace)), loop_rig.root)


async def test_the_builtin_loop_satisfies_the_agent_loop_seam(loop_rig) -> None:
    from forgepilot.seams.agent_loop import AgentLoop

    loop = ForgePilotAgentLoop(kernel_ctx=loop_rig.root)
    assert isinstance(loop, AgentLoop)
