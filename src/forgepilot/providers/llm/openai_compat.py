"""OpenAI-compatible chat provider — service ``llm``.

Targets the ``/chat/completions`` wire format, which is what self-hosted stacks,
internal gateways, vLLM and Ollama all speak. That is why it is the first real
provider: the company's own model is expected to appear behind the same protocol,
so this code is reused rather than replaced when it lands.

Split deliberately into two halves:

- **Translation** (:func:`to_payload`, :func:`parse_sse_line`) — pure functions
  from our types to the wire and back. Fully testable offline, and tested.
- **Transport** (:class:`OpenAICompatLLMPlugin`) — the HTTP call. Not testable
  without an endpoint, so it is kept thin enough to read in one sitting.

Keeping them apart is the point: the half that is easy to get wrong (delta
accumulation, tool-call indexing, usage extraction) is the half that has tests.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
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
    Usage,
    drain_stream,
)

__all__ = [
    "OpenAICompatLLMPlugin",
    "OpenAICompatError",
    "to_payload",
    "parse_sse_line",
    "iter_chunks",
]

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatError(RuntimeError):
    """The endpoint refused the request or returned something unusable."""


# ---------------------------------------------------------------------------
# Translation: ours -> theirs
# ---------------------------------------------------------------------------
def _message_payload(message: ChatMessage) -> dict[str, Any]:
    if message.role is Role.TOOL:
        # `tool_call_id` is mandatory here; an endpoint that receives a tool
        # message without one generally 400s, and silently dropping it would make
        # the failure look like a prompt problem.
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content or "",
        }

    if message.role is Role.ASSISTANT and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments_json},
                }
                for call in message.tool_calls
            ],
        }

    return {"role": str(message.role), "content": message.content or ""}


def to_payload(request: ChatRequest, *, stream: bool = True, include_usage: bool = True) -> dict[str, Any]:
    """Build the ``/chat/completions`` body.

    System sections are joined into a single leading system message: the wire
    format has one, and pretending otherwise by emitting several would rely on
    unspecified endpoint behaviour.
    """
    messages: list[dict[str, Any]] = []
    if request.system:
        messages.append({"role": "system", "content": "\n\n".join(request.system)})
    messages.extend(_message_payload(m) for m in request.messages)

    payload: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.parameters),
                },
            }
            for tool in request.tools
        ]
    if stream:
        payload["stream"] = True
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
    return payload


# ---------------------------------------------------------------------------
# Translation: theirs -> ours
# ---------------------------------------------------------------------------
def parse_sse_line(line: str) -> dict[str, Any] | None:
    """Parse one SSE line into a JSON object, or ``None`` if it carries nothing.

    ``None`` covers blank keep-alive lines, comments, and the ``[DONE]`` sentinel.
    Malformed JSON raises rather than being skipped: a stream we cannot parse is a
    stream we cannot faithfully log, and guessing would break replay.
    """
    text = line.strip()
    if not text or text.startswith(":"):
        return None
    if text.startswith("data:"):
        text = text[len("data:") :].strip()
    if not text or text == "[DONE]":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpenAICompatError(
            f"could not parse a streamed chunk: {exc}. The chunk is discarded rather "
            f"than guessed at, because an event we cannot read is an event the log "
            f"cannot reproduce."
        ) from exc


def chunk_from_delta(event: Mapping[str, Any]) -> tuple[list[StreamChunk], Usage | None]:
    """Turn one streamed event into chunks, plus usage if the event carried it.

    Handles the shape every OpenAI-compatible endpoint uses: text arrives as
    ``delta.content``, tool calls as ``delta.tool_calls`` indexed across events
    (the first event for an index carries id and name, later ones only argument
    fragments).
    """
    chunks: list[StreamChunk] = []

    choices = event.get("choices") or []
    for choice in choices:
        delta = choice.get("delta") or {}

        content = delta.get("content")
        if content:
            chunks.append(StreamChunk(kind="text", text=str(content)))

        reasoning = delta.get("reasoning_content")
        if reasoning:
            chunks.append(StreamChunk(kind="reasoning", text=str(reasoning)))

        for call in delta.get("tool_calls") or []:
            function = call.get("function") or {}
            chunks.append(
                StreamChunk(
                    kind="tool_call_delta",
                    tool_index=int(call.get("index", 0)),
                    tool_call_id=call.get("id"),
                    tool_name=function.get("name"),
                    arguments_delta=str(function.get("arguments") or ""),
                )
            )

    usage = None
    raw_usage = event.get("usage")
    if isinstance(raw_usage, Mapping):
        usage = Usage(
            input_tokens=int(raw_usage.get("prompt_tokens", 0)),
            output_tokens=int(raw_usage.get("completion_tokens", 0)),
        )

    return chunks, usage


async def iter_chunks(lines: AsyncIterator[str]) -> AsyncIterator[StreamChunk]:
    """Fold a stream of SSE lines into our chunk vocabulary."""
    async for line in lines:
        event = parse_sse_line(line)
        if event is None:
            continue
        chunks, usage = chunk_from_delta(event)
        for chunk in chunks:
            yield chunk
        if usage is not None:
            yield StreamChunk(kind="usage", usage=usage)
    yield StreamChunk(kind="done")


def chunks_from_events(events: Iterable[Mapping[str, Any]]) -> list[StreamChunk]:
    """Synchronous convenience for tests and fixtures."""
    out: list[StreamChunk] = []
    for event in events:
        chunks, usage = chunk_from_delta(event)
        out.extend(chunks)
        if usage is not None:
            out.append(StreamChunk(kind="usage", usage=usage))
    out.append(StreamChunk(kind="done"))
    return out


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class OpenAICompatLLMPlugin(PluginService):
    """Serves ``llm`` against an OpenAI-compatible endpoint."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})

        self.model: str = str(cfg.get("model", "gpt-4o-mini"))
        self.base_url: str = str(
            cfg.get("base_url") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        # The key is read from the environment by *name*, so the value never
        # lands in a profile file or in `--dump-config`.
        self._key_env: str = str(cfg.get("api_key_env", "OPENAI_API_KEY"))
        self.timeout: float = float(cfg.get("timeout_s", 120.0))
        self.include_usage: bool = bool(cfg.get("include_usage", True))
        self._max_context: int = int(cfg.get("max_context_tokens", 128_000))

        self._client: Any = None

    # -- credentials -------------------------------------------------------
    @property
    def api_key(self) -> str:
        value = os.environ.get(self._key_env)
        if not value:
            raise OpenAICompatError(
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
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def dispose(self) -> None:  # pragma: no cover - lifecycle
        if self._client is not None:
            self._client = None

    # -- LLMService --------------------------------------------------------
    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            tool_calling=True,
            streaming=True,
            max_context_tokens=self._max_context,
            supports_system_prompt=True,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        payload = to_payload(request, stream=True, include_usage=self.include_usage)
        client = self._http()

        async with client.stream("POST", "/chat/completions", json=payload) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise OpenAICompatError(
                    f"{self.base_url}/chat/completions returned {response.status_code}: "
                    f"{body[:500]}"
                )
            async for chunk in iter_chunks(_aiter_lines(response)):
                yield chunk

    async def complete(self, request: ChatRequest) -> ChatResponse:
        collected: list[StreamChunk] = []
        async for chunk in self.stream(request):
            collected.append(chunk)
        return drain_stream(collected)


async def _aiter_lines(response: Any) -> AsyncIterator[str]:
    """Yield decoded lines from a streaming httpx response."""
    async for line in response.aiter_lines():
        yield line
