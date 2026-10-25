"""A model that answers instantly — service ``llm``.

**This is not a model.** It exists so that a load test can measure *the server* rather than a
vendor: with the default profile the `llm` row points at a real endpoint, and a turn then costs
1.4 s of somebody else's latency plus real money. A single-replica load test that includes the
network cannot tell you where your own ceiling is, and one that spends money per run is a test
nobody runs.

So: this provider returns one text turn immediately, records every request it was given (the
same affordance `ReplayLLMPlugin` offers, and for the same reason — a test may want to assert
what was sent), and reports zero cost. It is deliberately **not** in any profile: a deployment
that selected it would be a deployment with no model at all, and `--boot` would look healthy
while every answer was a canned sentence.

Its usage numbers are zeros on purpose. Reporting a plausible-looking token count would make
`turn/usage` events in a load run indistinguishable from a real run's, and the price in the log
of a benchmark that called nobody should be exactly zero.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.llm import (
    SERVICE_NAME,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    LLMCapabilities,
    StreamChunk,
    Usage,
)

__all__ = ["EchoLLMPlugin"]

#: What every answer says. Fixed, so a load run's turns are identical and its numbers comparable
#: between runs; the text never reaches a user because nothing selects this provider but a bench.
ANSWER = "the echo provider answered without a model"


class EchoLLMPlugin(PluginService):
    """Answers every request at once, with one text turn and zero usage."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self.model: str = str(cfg.get("model", "echo-0"))
        #: Every request this provider has seen, in order — public on purpose.
        self.requests: list[ChatRequest] = []

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            tool_calling=False,
            streaming=True,
            max_context_tokens=200_000,
            supports_system_prompt=True,
        )

    def chunks(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)

        async def _generate() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(kind="text", text=ANSWER)
            yield StreamChunk(kind="usage", usage=Usage())
            yield StreamChunk(kind="done")

        return _generate()

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(
            message=ChatMessage(role="assistant", content=ANSWER),
            usage=Usage(),
        )
