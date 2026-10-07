"""A scripted LLM — service ``llm``.

This is the provider CI runs against. **No test may hit a live model**: an
unscripted model makes tests non-deterministic, slow, and dependent on
credentials, and it hides prompt-assembly bugs behind model tolerance.

It is also a genuine recording: every request is kept, so a test can assert what
the runtime *sent*, not merely what came back.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Iterable
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.llm import (
    SERVICE_NAME,
    ChatRequest,
    ChatResponse,
    LLMCapabilities,
    StreamChunk,
    Usage,
    drain_stream,
)

__all__ = ["ReplayLLMPlugin", "text_turn", "tool_call_turn"]


def text_turn(text: str, *, usage: Usage | None = None) -> tuple[StreamChunk, ...]:
    """A scripted assistant turn that only produces text."""
    chunks = [StreamChunk(kind="text", text=text)]
    if usage is not None:
        chunks.append(StreamChunk(kind="usage", usage=usage))
    chunks.append(StreamChunk(kind="done"))
    return tuple(chunks)


def tool_call_turn(
    name: str,
    arguments_json: str,
    *,
    call_id: str = "call-1",
    text: str = "",
) -> tuple[StreamChunk, ...]:
    """A scripted assistant turn that requests one tool call.

    ``arguments_json`` is passed through verbatim, so a test can script malformed
    arguments on purpose and check the failure path.
    """
    chunks: list[StreamChunk] = []
    if text:
        chunks.append(StreamChunk(kind="text", text=text))
    chunks.append(
        StreamChunk(
            kind="tool_call_delta",
            tool_index=0,
            tool_call_id=call_id,
            tool_name=name,
            arguments_delta=arguments_json,
        )
    )
    chunks.append(StreamChunk(kind="done"))
    return tuple(chunks)


class ReplayLLMPlugin(PluginService):
    """Serves ``llm`` from a queue of pre-scripted turns.

    When the script runs out, the next call raises rather than inventing a
    response: a test that consumes more turns than it scripted has a bug, and
    failing loudly is the only way that surfaces.
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self.model: str = str(cfg.get("model", "replay-1"))
        self._turns: deque[tuple[StreamChunk, ...]] = deque()
        #: Every request this provider has seen, in order. Public on purpose:
        #: asserting on it is how prompt-assembly bugs get caught.
        self.requests: list[ChatRequest] = []

    # -- scripting ---------------------------------------------------------
    def script(self, *turns: Iterable[StreamChunk] | str) -> "ReplayLLMPlugin":
        """Queue turns. A plain string becomes a text-only turn."""
        for turn in turns:
            self._turns.append(text_turn(turn) if isinstance(turn, str) else tuple(turn))
        return self

    def reset(self) -> None:
        self._turns.clear()
        self.requests.clear()

    @property
    def remaining(self) -> int:
        return len(self._turns)

    # -- LLMService --------------------------------------------------------
    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            tool_calling=True,
            streaming=True,
            max_context_tokens=200_000,
            supports_system_prompt=True,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        if not self._turns:
            raise AssertionError(
                "ReplayLLM ran out of scripted turns: "
                f"request #{len(self.requests)} for model {request.model!r} "
                f"had {len(request.messages)} messages and no scripted response. "
                "Script another turn or the test is asking for more than it set up."
            )
        for chunk in self._turns.popleft():
            yield chunk

    async def complete(self, request: ChatRequest) -> ChatResponse:
        chunks: list[StreamChunk] = []
        async for chunk in self.stream(request):
            chunks.append(chunk)
        return drain_stream(chunks)
