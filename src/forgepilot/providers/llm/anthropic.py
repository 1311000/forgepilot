"""Anthropic Messages API provider — service ``llm``.

A second wire format, and the reason it exists is the seam's own premise: the provider
is *expected* to change (a commercial model today, the company's own later), so "nothing
downstream may depend on which one is installed" has to be demonstrated by a second
provider rather than asserted by the shape of the first.

It is **not** a copy with different field names. Three differences are structural, and
each one is a place where a naive port would lose information:

1. **Tool calls are content blocks, not a sibling array.** An assistant turn interleaves
   text and ``tool_use`` blocks in one ``content`` list, so a message with both is one
   message here and two fields there. The order of the blocks is the order the model
   emitted them, and re-ordering them would re-order the reasoning.
2. **A tool result is a *user* message.** There is no ``tool`` role. Consecutive results
   must be folded into a single user turn, because the API rejects two user turns in a row.
3. **``input`` is an object, not a JSON string.** Our `ToolCallRequest.arguments_json` is
   kept unparsed on purpose (a malformed argument must be replayable from the log), so this
   provider parses it at the boundary and **refuses** a request it cannot translate, naming
   the call — sending ``{}`` instead would turn a malformed call into an empty one.

Same split as the OpenAI-compatible provider: pure translation with tests, a thin transport
that is not testable without an endpoint.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.llm import (
    SERVICE_NAME,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    LLMCapabilities,
    Role,
    StreamChunk,
    ToolSchema,
    Usage,
    drain_stream,
)

__all__ = [
    "AnthropicError",
    "AnthropicLLMPlugin",
    "DEFAULT_API_VERSION",
    "DEFAULT_BASE_URL",
    "chunk_from_event",
    "chunks_from_events",
    "iter_chunks",
    "parse_sse_line",
    "to_payload",
]

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"

#: Pinned, because the API is versioned by header and an unversioned request is a
#: request whose shape can change under the deployment.
DEFAULT_API_VERSION = "2023-06-01"


class AnthropicError(RuntimeError):
    """The endpoint refused the request, or the request could not be translated."""


# ---------------------------------------------------------------------------
# Translation: ours -> theirs
# ---------------------------------------------------------------------------
def _text_blocks(text: str | None) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}] if text else []


def _tool_use_blocks(tool_calls: Sequence[Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for call in tool_calls:
        try:
            arguments = json.loads(call.arguments_json or "{}")
        except json.JSONDecodeError as exc:
            raise AnthropicError(
                f"tool call {call.call_id!r} ({call.name}) has arguments that are not "
                f"JSON: {exc}. The Messages API takes an object, and sending an empty "
                f"one would turn a malformed call into a different call."
            ) from exc
        if not isinstance(arguments, dict):
            raise AnthropicError(
                f"tool call {call.call_id!r} ({call.name}) has arguments that are "
                f"{type(arguments).__name__}, not an object"
            )
        blocks.append(
            {"type": "tool_use", "id": call.call_id, "name": call.name, "input": arguments}
        )
    return blocks


def _tool_results(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_result",
            "tool_use_id": message.tool_call_id or "",
            "content": message.content or "",
        }
        for message in messages
    ]


def to_payload(request: ChatRequest, *, stream: bool = True) -> dict[str, Any]:
    """Build the ``/messages`` body.

    System text is a top-level field, and a ``Role.SYSTEM`` message in ``messages`` is
    folded into it: the API has no system role inside the conversation, and leaving one
    there is a 400 rather than a correction.
    """
    system_parts = list(request.system)
    messages: list[dict[str, Any]] = []

    index = 0
    while index < len(request.messages):
        message = request.messages[index]
        if message.role is Role.SYSTEM:
            if message.content:
                system_parts.append(message.content)
            index += 1
            continue

        if message.role is Role.TOOL:
            # One user turn for a run of results: consecutive user messages are refused.
            run: list[ChatMessage] = []
            while index < len(request.messages) and request.messages[index].role is Role.TOOL:
                run.append(request.messages[index])
                index += 1
            messages.append({"role": "user", "content": _tool_results(run)})
            continue

        if message.role is Role.ASSISTANT and message.tool_calls:
            blocks = _text_blocks(message.content) + _tool_use_blocks(message.tool_calls)
            messages.append({"role": "assistant", "content": blocks})
            index += 1
            continue

        messages.append({"role": str(message.role), "content": _text_blocks(message.content)})
        index += 1

    payload: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if request.tools:
        payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.parameters),
            }
            for tool in request.tools
        ]
    if stream:
        payload["stream"] = True
    return payload


def tool_schema_payload(tools: Iterable[ToolSchema]) -> list[dict[str, Any]]:
    """Tools as this wire format declares them — ``input_schema``, not ``parameters``."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": dict(tool.parameters),
        }
        for tool in tools
    ]


# ---------------------------------------------------------------------------
# Translation: theirs -> ours
# ---------------------------------------------------------------------------
def parse_sse_line(line: str) -> Mapping[str, Any] | None:
    """One ``data:`` line of an event stream, or ``None`` for anything else.

    The ``event:`` lines are ignored on purpose: the payload's own ``type`` field says
    the same thing, and trusting the two to agree is trusting a second source.
    """
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise AnthropicError(f"the event stream carried a non-JSON payload: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise AnthropicError("the event stream carried something that is not an object")
    return parsed


@dataclass
class _StreamState:
    """Usage is split across two events, so it has to be accumulated.

    ``message_start`` carries input tokens, ``message_delta`` carries output tokens, and
    `drain_stream` replaces its usage wholesale on each usage chunk — emitting either one
    alone would lose the other.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    stop_reason: str = "end_turn"
    calls: dict[int, dict[str, Any]] = field(default_factory=dict)

    def usage(self) -> Usage:
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
        )


def chunk_from_event(event: Mapping[str, Any], state: _StreamState) -> list[StreamChunk]:
    """One protocol event → our chunk vocabulary.

    Pure, and takes the accumulator explicitly, so a test can drive a whole stream through
    it without a socket — the half that is easy to get wrong is the half with tests.
    """
    kind = event.get("type")

    if kind == "message_start":
        usage = (event.get("message") or {}).get("usage") or {}
        state.input_tokens = int(usage.get("input_tokens") or 0)
        state.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
        return []

    if kind == "content_block_start":
        block = event.get("content_block") or {}
        index = int(event.get("index") or 0)
        if block.get("type") == "tool_use":
            state.calls[index] = {
                "id": block.get("id") or "",
                "name": block.get("name") or "",
            }
            return [
                StreamChunk(
                    kind="tool_call_delta",
                    tool_index=index,
                    tool_call_id=state.calls[index]["id"],
                    tool_name=state.calls[index]["name"],
                )
            ]
        return []

    if kind == "content_block_delta":
        delta = event.get("delta") or {}
        index = int(event.get("index") or 0)
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            return [StreamChunk(kind="text", text=str(delta.get("text") or ""))]
        if delta_type == "thinking_delta":
            return [StreamChunk(kind="reasoning", text=str(delta.get("thinking") or ""))]
        if delta_type == "input_json_delta":
            return [
                StreamChunk(
                    kind="tool_call_delta",
                    tool_index=index,
                    arguments_delta=str(delta.get("partial_json") or ""),
                )
            ]
        return []

    if kind == "message_delta":
        usage = event.get("usage") or {}
        if usage.get("output_tokens") is not None:
            state.output_tokens = int(usage["output_tokens"])
        stop_reason = event.get("delta", {}).get("stop_reason")
        if isinstance(stop_reason, str) and stop_reason:
            state.stop_reason = stop_reason
        return []

    if kind == "message_stop":
        return [StreamChunk(kind="usage", usage=state.usage())]

    # `ping`, `content_block_stop`, `error` (surfaced by the transport) and anything
    # added later: an unknown event is not an error, because the protocol says so.
    return []


def chunks_from_events(events: Iterable[Mapping[str, Any]]) -> list[StreamChunk]:
    """Synchronous convenience for tests and fixtures."""
    state = _StreamState()
    out: list[StreamChunk] = []
    for event in events:
        out.extend(chunk_from_event(event, state))
    out.append(StreamChunk(kind="done"))
    return out


async def iter_chunks(lines: AsyncIterator[str]) -> AsyncIterator[StreamChunk]:
    """Fold a stream of SSE lines into our chunk vocabulary."""
    state = _StreamState()
    async for line in lines:
        event = parse_sse_line(line)
        if event is None:
            continue
        if event.get("type") == "error":
            error = event.get("error") or {}
            raise AnthropicError(
                f"the endpoint reported {error.get('type')!r} mid-stream: {error.get('message')!r}"
            )
        for chunk in chunk_from_event(event, state):
            yield chunk
    yield StreamChunk(kind="done")


async def _aiter_lines(response: Any) -> AsyncIterator[str]:
    async for line in response.aiter_lines():
        yield line


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class AnthropicLLMPlugin(PluginService):
    """Serves ``llm`` against the Anthropic Messages API."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})

        self.model: str = str(cfg.get("model", "claude-sonnet-4-5"))
        self.base_url: str = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        # The key is read from the environment by *name*, so its value never lands in a
        # profile file or in `--dump-config` — the same rule the OpenAI row follows.
        self._key_env: str = str(cfg.get("api_key_env", "ANTHROPIC_API_KEY"))
        self.version: str = str(cfg.get("api_version", DEFAULT_API_VERSION))
        self.timeout: float = float(cfg.get("timeout_s", 120.0))
        self._max_context: int = int(cfg.get("max_context_tokens", 200_000))
        self._price_in: float = float(cfg.get("price_per_1k_input", 0.0))
        self._price_out: float = float(cfg.get("price_per_1k_output", 0.0))
        self._client: Any = None

    @property
    def api_key(self) -> str:
        value = os.environ.get(self._key_env)
        if not value:
            raise AnthropicError(
                f"{self._key_env} is not set. The key is read from the environment by "
                f"name (config carries the variable's *name*, never its value) so that "
                f"it cannot leak through --dump-config."
            )
        return value

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": self.version,
                    "content-type": "application/json",
                },
            )
        return self._client

    def dispose(self) -> None:  # pragma: no cover - lifecycle
        self._client = None

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            tool_calling=True,
            streaming=True,
            max_context_tokens=self._max_context,
            supports_system_prompt=True,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        payload = to_payload(request, stream=True)
        client = self._http()
        async with client.stream("POST", "/messages", json=payload) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise AnthropicError(
                    f"{self.base_url}/messages returned {response.status_code}: {body[:500]}"
                )
            async for chunk in iter_chunks(_aiter_lines(response)):
                if chunk.kind == "usage" and chunk.usage is not None:
                    yield replace(chunk, usage=self._priced(chunk.usage))
                else:
                    yield chunk

    def _priced(self, usage: Usage) -> Usage:
        if not (self._price_in or self._price_out):
            return usage
        return replace(
            usage,
            cost_usd=(
                usage.input_tokens * self._price_in / 1000.0
                + usage.output_tokens * self._price_out / 1000.0
            ),
        )

    async def complete(self, request: ChatRequest) -> ChatResponse:
        return drain_stream([chunk async for chunk in self.stream(request)])
