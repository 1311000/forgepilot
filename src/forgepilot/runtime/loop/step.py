"""One step: a model request, then the tools it asked for.

The request is **derived from the session log**, never accumulated in memory. A
loop that kept its own message list would be a second source of truth, and the
two would diverge the first time anything edited one without the other — which is
exactly the failure the log-first design exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgepilot.errors import ForgePilotError
from forgepilot.runtime.log_guard import guarded_stream
from forgepilot.runtime.projection import build_request
from forgepilot.seams.llm import ChatMessage, StreamChunk, ToolCallRequest, ToolSchema, Usage
from forgepilot.seams.refs import TurnId
from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.session import EventKind
from forgepilot.seams.tools import ToolCall

__all__ = ["StepResult", "run_step", "accumulate", "ContextWindowExceeded"]


class ContextWindowExceeded(ForgePilotError):
    """A request would not fit the model's context window.

    Raised rather than truncated. Truncating would not breach *model-visible means
    logged* — the truncation itself would be a logged event — but it changes what
    the model sees, and picking a strategy before there is any real usage to
    calibrate against would be guessing. Compaction is a design task of its own.
    """


@dataclass(frozen=True, slots=True)
class StepResult:
    """What one model round-trip produced."""

    text: str
    tool_calls: tuple[ToolCallRequest, ...] = ()
    usage: Usage = field(default_factory=Usage)
    #: A reasoning model's thinking. Kept separate from ``text`` on purpose: it
    #: is recorded for audit but never becomes an assistant message, because it
    #: is not part of the conversation the model is asked to continue.
    reasoning: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def accumulate(chunks: list[StreamChunk]) -> StepResult:
    """Fold streamed chunks into a step result.

    Tools are keyed by ``tool_index`` rather than by position in the stream,
    because an endpoint may interleave fragments for several calls: the first
    event for an index carries id and name, later ones only argument text.
    Concatenating in arrival order would corrupt every call after the first.
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    slots: dict[int, dict[str, str]] = {}
    usage = Usage()

    for chunk in chunks:
        if chunk.kind == "text":
            text_parts.append(chunk.text)
        elif chunk.kind == "reasoning":
            reasoning_parts.append(chunk.text)
        elif chunk.kind == "tool_call_delta":
            slot = slots.setdefault(chunk.tool_index or 0, {"id": "", "name": "", "args": ""})
            if chunk.tool_call_id:
                slot["id"] = chunk.tool_call_id
            if chunk.tool_name:
                slot["name"] = chunk.tool_name
            slot["args"] += chunk.arguments_delta
        elif chunk.kind == "usage" and chunk.usage is not None:
            usage = chunk.usage

    calls = tuple(
        ToolCallRequest(call_id=slot["id"], name=slot["name"], arguments_json=slot["args"])
        for _, slot in sorted(slots.items())
        if slot["name"]
    )
    return StepResult(
        text="".join(text_parts),
        tool_calls=calls,
        usage=usage,
        reasoning="".join(reasoning_parts),
    )


def estimate_tokens(messages: tuple[ChatMessage, ...], system: tuple[str, ...]) -> int:
    """A deliberately crude size estimate — roughly four characters per token.

    Crude on purpose: a real tokenizer is a dependency and a per-model decision,
    and this exists only to refuse a request that would obviously blow the window
    rather than to bill anyone. When it is wrong it is wrong in the direction of
    refusing early, which is the safe direction.
    """
    total = sum(len(part) for part in system)
    for message in messages:
        total += len(message.content or "")
        for call in message.tool_calls:
            total += len(call.arguments_json) + len(call.name)
    return total // 4


async def run_step(
    *,
    session: Any,
    llm: Any,
    tools: Any,
    constraints: ExecutionConstraints,
    turn: TurnId,
    step: int,
    workspace_root: Path | None = None,
    model: str,
    system_sections: tuple[str, ...] = (),
    max_tokens: int = 4096,
    temperature: float = 0.0,
    tool_schemas: tuple[ToolSchema, ...] = (),
) -> StepResult:
    """Run one step, recording everything the model saw and did."""
    await session.step_start(turn, step)

    request = await build_request(
        session,
        model=model,
        system_sections=system_sections,
        turn=turn,
        step=step,
        tools=tool_schemas,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    capabilities = llm.capabilities()
    estimated = estimate_tokens(request.messages, request.system)
    if estimated > capabilities.max_context_tokens:
        raise ContextWindowExceeded(
            f"this request is roughly {estimated} tokens against a window of "
            f"{capabilities.max_context_tokens}. Refusing rather than truncating: "
            f"dropping history would change what the model sees, and doing that "
            f"silently is the failure the session log exists to prevent. Compaction "
            f"is not implemented yet."
        )

    chunks: list[StreamChunk] = []
    async for chunk in guarded_stream(session, llm, request):
        chunks.append(chunk)
    result = accumulate(chunks)

    if result.reasoning:
        # Log-only: durable for audit, never replayed as conversation. A
        # reasoning model can spend most of its output here, and dropping it
        # would lose the account of how the answer was reached.
        await session.append(
            EventKind.ASSISTANT_REASONING,
            {"text": result.reasoning},
            turn=turn,
            step=step,
        )

    await session.assistant_message(
        turn,
        step,
        result.text,
        tool_calls=[
            {"callId": c.call_id, "name": c.name, "arguments": c.arguments_json}
            for c in result.tool_calls
        ],
        usage={
            "inputTokens": result.usage.input_tokens,
            "outputTokens": result.usage.output_tokens,
            "cacheReadTokens": result.usage.cache_read_tokens,
        },
    )

    if not result.wants_tools:
        await session.step_end(turn, step)
        return result

    # Every requested call goes through the pipeline, including ones that will be
    # refused. A refusal is a result the model must see, or it will ask again.
    for requested in result.tool_calls:
        await tools.invoke(
            ToolCall(
                call_id=requested.call_id,
                name=requested.name,
                arguments_json=requested.arguments_json,
            ),
            recorder=session,
            turn=turn,
            step=step,
            constraints=constraints,
            workspace_root=workspace_root,
        )

    await session.step_end(turn, step)
    return result
