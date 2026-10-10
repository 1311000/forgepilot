"""LLM seam — service name ``llm``.

The provider selected here is expected to change: a commercial model today, the
company's self-trained local model later, exposed through the same interface.
Nothing downstream may depend on which one is installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = [
    "SERVICE_NAME",
    "Role",
    "ToolSchema",
    "ToolCallRequest",
    "ChatMessage",
    "ChatRequest",
    "Usage",
    "StreamChunk",
    "ChatResponse",
    "LLMCapabilities",
    "LLMService",
]

SERVICE_NAME = "llm"


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """A tool as the model sees it."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """A tool call the model asked for."""

    call_id: str
    name: str
    #: The raw, unparsed argument JSON exactly as the model emitted it.
    #: Kept unparsed on purpose so that a malformed-argument failure is
    #: replayable from the session log instead of being lost at parse time.
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    tool_call_id: str | None = None
    #: Which session-log event produced this message. Not model-visible: it
    #: exists so the log guard can prove every model input came from the log.
    source_event_seq: int | None = None


@dataclass(frozen=True, slots=True)
class ChatRequest:
    model: str
    system: tuple[str, ...]
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSchema, ...] = ()
    max_tokens: int = 4096
    temperature: float = 0.0
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    #: What this call cost, **attached by the provider** — it is the only thing
    #: that knows which model answered, and the deployment's prices are a fact
    #: about that endpoint in the same way `model` and `base_url` are. Zero when
    #: the provider was given no rates, which is why the budget guard's cost check
    #: is only meaningful on a row that has them.
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class StreamChunk:
    kind: Literal["text", "reasoning", "tool_call_delta", "usage", "done"]
    text: str = ""
    tool_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str = ""
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class ChatResponse:
    message: ChatMessage
    usage: Usage
    stop_reason: str = "end_turn"


@dataclass(frozen=True, slots=True)
class LLMCapabilities:
    tool_calling: bool = True
    streaming: bool = True
    max_context_tokens: int = 200_000
    supports_system_prompt: bool = True


@runtime_checkable
class LLMService(Protocol):
    """A model endpoint.

    ``stream`` is the primitive; ``complete`` is a convenience with a default
    built on it. Providers must implement ``stream``.
    """

    def capabilities(self) -> LLMCapabilities: ...

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]: ...

    async def complete(self, request: ChatRequest) -> ChatResponse: ...


def drain_stream(chunks: Sequence[StreamChunk]) -> ChatResponse:
    """Fold streamed chunks into a single response.

    Shared by providers so a naive implementation cannot quietly disagree with
    ``complete`` about how deltas assemble.
    """
    text_parts: list[str] = []
    tool_calls: dict[int, dict[str, str]] = {}
    usage = Usage()
    stop_reason = "end_turn"

    for chunk in chunks:
        if chunk.kind == "text":
            text_parts.append(chunk.text)
        elif chunk.kind == "tool_call_delta":
            slot = tool_calls.setdefault(
                chunk.tool_index or 0, {"id": "", "name": "", "args": ""}
            )
            if chunk.tool_call_id:
                slot["id"] = chunk.tool_call_id
            if chunk.tool_name:
                slot["name"] = chunk.tool_name
            slot["args"] += chunk.arguments_delta
        elif chunk.kind == "usage" and chunk.usage is not None:
            usage = chunk.usage
        elif chunk.kind == "done":
            stop_reason = "end_turn"

    calls = tuple(
        ToolCallRequest(call_id=slot["id"], name=slot["name"], arguments_json=slot["args"])
        for _, slot in sorted(tool_calls.items())
    )
    message = ChatMessage(
        role=Role.ASSISTANT,
        content="".join(text_parts) or None,
        tool_calls=calls,
    )
    return ChatResponse(message=message, usage=usage, stop_reason=stop_reason)
