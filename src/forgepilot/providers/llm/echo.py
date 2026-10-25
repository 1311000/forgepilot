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

`delay_s` is a test instrument, not a feature: a canned answer arrives in microseconds, so a
test that needs to observe a turn *while it is running* — the two-replica test observing the
lease from the other process — has no window unless the answer is held open. Zero by default.
"""

from __future__ import annotations

import asyncio
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
        delay = cfg.get("delay_s", 0.0)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0:
            raise ValueError(f"delay_s must be a number of seconds, not {delay!r}")
        self.delay_s: float = float(delay)
        #: Every request this provider has seen, in order — public on purpose.
        self.requests: list[ChatRequest] = []

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            tool_calling=False,
            streaming=True,
            max_context_tokens=200_000,
            supports_system_prompt=True,
        )

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """The seam's streaming method -- `stream`, not `chunks`.

        The first version of this provider offered `chunks`, which is not a method the protocol
        declares: `log_guard.guarded_stream` calls `llm.stream(...)`, so every echo turn ended
        with `turn/end {"reason": "error"}` and the load test that introduced this provider
        measured a failing path. Its unit tests passed because they called `chunks` directly --
        a test of a method nobody calls.
        """
        self.requests.append(request)

        async def _generate() -> AsyncIterator[StreamChunk]:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            yield StreamChunk(kind="text", text=ANSWER)
            yield StreamChunk(kind="usage", usage=Usage())
            yield StreamChunk(kind="done")

        return _generate()

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return ChatResponse(
            message=ChatMessage(role="assistant", content=ANSWER),
            usage=Usage(),
        )
