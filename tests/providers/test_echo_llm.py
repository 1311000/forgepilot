"""P30: the zero-latency model, and the one thing it must not do.

`EchoLLMPlugin` exists so a load run measures the replica. Three properties matter, and the
third is the one that keeps a benchmark honest:

* it answers immediately and records every request (a test may want to assert what was sent);
* it advertises **no tool calling** — a model that cannot ask for a tool is a turn that ends in
  one step, which is exactly what a load run wants;
* its usage is **zero**, not a plausible-looking number. A `turn/usage` event from a benchmark
  that called nobody must not be indistinguishable from a real one's.
"""

from __future__ import annotations

import pytest

from forgepilot.kernel import create_root
from forgepilot.providers.llm.echo import ANSWER, EchoLLMPlugin
from forgepilot.seams.llm import ChatMessage, ChatRequest, drain_stream


@pytest.fixture
async def provider():
    root = create_root()
    await root.plugin(EchoLLMPlugin, {"model": "echo-0"})
    yield root.llm


def a_request() -> ChatRequest:
    return ChatRequest(
        model="echo-0", system=("be brief",), messages=(ChatMessage(role="user", content="hi"),)
    )


@pytest.mark.asyncio
async def test_it_answers_with_one_text_turn(provider) -> None:
    response = await provider.complete(a_request())
    assert response.message.content == ANSWER
    assert response.message.role == "assistant"
    assert response.message.tool_calls == ()


@pytest.mark.asyncio
async def test_it_advertises_no_tool_calling(provider) -> None:
    """A model that cannot request a tool is a one-step turn — what a load run wants."""
    assert provider.capabilities().tool_calling is False
    assert provider.capabilities().streaming is True


@pytest.mark.asyncio
async def test_its_usage_is_zero_rather_than_plausible(provider) -> None:
    response = drain_stream([chunk async for chunk in provider.chunks(a_request())])
    usage = response.usage
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cost_usd == 0.0


@pytest.mark.asyncio
async def test_it_records_every_request_it_was_given(provider) -> None:
    await provider.complete(a_request())
    await provider.complete(a_request())
    assert len(provider.requests) == 2
    assert provider.requests[0].messages[0].content == "hi"
