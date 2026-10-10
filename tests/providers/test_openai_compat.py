"""OpenAI-compatible provider: translation, and the transport against a real server.

The provider is split into pure translation and a thin transport, and both halves
are tested here. The transport tests run against **a real HTTP server on a real
socket** speaking the wire format — not a mocked client. That is deliberate: the
part most likely to be wrong is SSE framing and streaming reads, and a mocked
client would happily accept whatever framing we invented.

What this file cannot test is a live endpoint's *behaviour* — whether a given
gateway honours ``stream_options``, or what an internal proxy does to the stream.
That needs the real endpoint, and is called out in CLAUDE.md as unverified.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from forgepilot.providers.llm.openai_compat import (
    OpenAICompatError,
    OpenAICompatLLMPlugin,
    apply_price,
    chunk_from_delta,
    chunks_from_events,
    iter_chunks,
    parse_sse_line,
    to_payload,
)
from forgepilot.seams.llm import (
    ChatMessage,
    ChatRequest,
    Role,
    ToolCallRequest,
    ToolSchema,
    Usage,
    drain_stream,
)

from forgepilot.kernel import create_root, dispose_fiber


def request_with(messages, *, system=(), tools=()) -> ChatRequest:
    return ChatRequest(
        model="test-model",
        system=tuple(system),
        messages=tuple(messages),
        tools=tuple(tools),
        max_tokens=256,
        temperature=0.0,
    )


# ---------------------------------------------------------------------------
# Translation: ours -> theirs
# ---------------------------------------------------------------------------
def test_system_sections_become_one_leading_system_message() -> None:
    payload = to_payload(request_with([ChatMessage(role=Role.USER, content="hi")], system=("a", "b")))

    assert payload["messages"][0] == {"role": "system", "content": "a\n\nb"}
    assert payload["messages"][1] == {"role": "user", "content": "hi"}


def test_a_tool_message_carries_its_call_id() -> None:
    """Omitting it is a 400 on most endpoints, and looks like a prompt bug."""
    payload = to_payload(
        request_with(
            [ChatMessage(role=Role.TOOL, content="ok", tool_call_id="call-1")]
        )
    )

    assert payload["messages"][0] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "ok",
    }


def test_an_assistant_message_with_tool_calls_is_shaped_correctly() -> None:
    payload = to_payload(
        request_with(
            [
                ChatMessage(
                    role=Role.ASSISTANT,
                    content=None,
                    tool_calls=(
                        ToolCallRequest(call_id="c1", name="read_file", arguments_json='{"path":"a"}'),
                    ),
                )
            ]
        )
    )

    call = payload["messages"][0]["tool_calls"][0]
    assert call == {
        "id": "c1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"a"}'},
    }


def test_tool_arguments_are_passed_through_unparsed() -> None:
    """A malformed argument string must reach the endpoint as-is.

    Repairing it here would turn a replayable failure into a mystery.
    """
    payload = to_payload(
        request_with(
            [
                ChatMessage(
                    role=Role.ASSISTANT,
                    tool_calls=(ToolCallRequest("c1", "t", "{not json"),),
                )
            ]
        )
    )

    assert payload["messages"][0]["tool_calls"][0]["function"]["arguments"] == "{not json"


def test_tools_are_advertised_in_the_expected_shape() -> None:
    schema = ToolSchema(
        name="read_file",
        description="read a file",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    payload = to_payload(request_with([], tools=(schema,)))

    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["function"]["name"] == "read_file"


def test_streaming_requests_ask_for_usage() -> None:
    payload = to_payload(request_with([]), stream=True, include_usage=True)

    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}


def test_a_non_streaming_payload_omits_stream_keys() -> None:
    payload = to_payload(request_with([]), stream=False)

    assert "stream" not in payload
    assert "stream_options" not in payload


# ---------------------------------------------------------------------------
# Translation: theirs -> ours
# ---------------------------------------------------------------------------
def test_sse_line_parsing_skips_noise_and_rejects_garbage() -> None:
    assert parse_sse_line("") is None
    assert parse_sse_line("   ") is None
    assert parse_sse_line(": keep-alive") is None
    assert parse_sse_line("data: [DONE]") is None
    assert parse_sse_line('data: {"a": 1}') == {"a": 1}
    assert parse_sse_line('{"a": 1}') == {"a": 1}

    with pytest.raises(OpenAICompatError, match="could not parse"):
        parse_sse_line("data: {not json")


def test_text_and_reasoning_deltas_are_both_captured() -> None:
    chunks, usage = chunk_from_delta(
        {"choices": [{"delta": {"content": "hi", "reasoning_content": "thinking"}}]}
    )

    assert [(c.kind, c.text) for c in chunks] == [("text", "hi"), ("reasoning", "thinking")]
    assert usage is None


def test_a_split_tool_call_reassembles_across_events() -> None:
    """The common shape: id and name first, argument fragments after."""
    chunks = chunks_from_events(
        [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": ""}}
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"path":'}}
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '"src/a.py"}'}}
            ]}}]},
        ]
    )

    response = drain_stream(chunks)

    assert len(response.message.tool_calls) == 1
    call = response.message.tool_calls[0]
    assert call.name == "read_file"
    assert call.arguments_json == '{"path":"src/a.py"}'


def test_usage_is_extracted_from_the_final_event() -> None:
    chunks = chunks_from_events(
        [
            {"choices": [{"delta": {"content": "x"}}]},
            {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
        ]
    )

    response = drain_stream(chunks)

    assert response.usage == Usage(input_tokens=11, output_tokens=7)
    assert response.message.content == "x"


# ---------------------------------------------------------------------------
# Pricing — the producer `max_cost_usd` never had
# ---------------------------------------------------------------------------
def test_a_configured_rate_becomes_a_cost() -> None:
    """Without this there was no writer for `usage.cost_usd` on any path, so the
    budget guard's cost check read a field nothing ever set — a limit that could
    not be reached, described as a guard because exceeding it "must be final".
    """
    priced = apply_price(
        Usage(input_tokens=1_000, output_tokens=500),
        price_per_1k_input=0.03,
        price_per_1k_output=0.06,
    )
    assert priced.cost_usd == pytest.approx(0.03 + 0.03)


def test_no_configured_rate_leaves_the_cost_alone() -> None:
    """Zero is the honest answer, not a guess.

    A deployment that has not said what a token costs has not said what the call
    cost, and inventing a figure would make a cost budget fire on a number nobody
    supplied. It does leave `max_cost_usd` unusable on that row — which is why the
    rates live on the same config row as the model, where `--dump-config` shows
    them.
    """
    usage = Usage(input_tokens=1_000, output_tokens=500)
    assert apply_price(usage, price_per_1k_input=0.0, price_per_1k_output=0.0) == usage


def test_cached_reads_are_not_priced_as_input() -> None:
    """Pinned as *behaviour*, not as a wish: `cache_read_tokens` is not billed at
    the input rate here. A cached read is cheaper than a fresh one everywhere that
    publishes rates, so pricing it as input would overstate the cost — the safe
    direction for a budget, but wrong, and it should be a deliberate change rather
    than something that drifts in.
    """
    priced = apply_price(
        Usage(input_tokens=100, output_tokens=0, cache_read_tokens=10_000),
        price_per_1k_input=0.30,
        price_per_1k_output=0.60,
    )
    assert priced.cost_usd == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# Transport, against a real socket
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - required name
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append((self.path, body, dict(self.headers)))

        if self.server.fail_status:
            payload = b'{"error": "nope"}'
            self.send_response(self.server.fail_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        for event in self.server.events:
            frame = f"data: {json.dumps(event)}\n\n".encode("utf-8")
            self.wfile.write(f"{len(frame):X}\r\n".encode())
            self.wfile.write(frame)
            self.wfile.write(b"\r\n")
        done = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(done):X}\r\n".encode())
        self.wfile.write(done)
        self.wfile.write(b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *args):  # silence the test server
        return


@pytest.fixture
def endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.events = []
    server.fail_status = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
async def provider(endpoint, monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret-value")
    root = create_root()
    fiber = await root.plugin(
        OpenAICompatLLMPlugin,
        {
            "model": "test-model",
            "base_url": f"http://127.0.0.1:{endpoint.server_port}/v1",
            "api_key_env": "TEST_LLM_KEY",
        },
    )
    try:
        yield root.llm
    finally:
        await dispose_fiber(fiber)


async def test_the_provider_streams_a_reply_over_a_real_socket(provider, endpoint) -> None:
    endpoint.events = [
        {"choices": [{"delta": {"content": "the answer "}}]},
        {"choices": [{"delta": {"content": "is 42"}}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}},
    ]

    response = await provider.complete(
        request_with([ChatMessage(role=Role.USER, content="q")])
    )

    assert response.message.content == "the answer is 42"
    assert response.usage == Usage(input_tokens=5, output_tokens=3)


async def test_the_provider_sends_a_bearer_token_and_the_expected_path(provider, endpoint) -> None:
    endpoint.events = [{"choices": [{"delta": {"content": "ok"}}]}]

    await provider.complete(request_with([ChatMessage(role=Role.USER, content="q")]))

    path, body, headers = endpoint.requests[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret-value"
    assert body["model"] == "test-model"
    assert body["messages"] == [{"role": "user", "content": "q"}]


async def test_a_tool_call_survives_the_wire(provider, endpoint) -> None:
    endpoint.events = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": ""}}
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"path":"src/a.py"}'}}
        ]}}]},
    ]

    response = await provider.complete(request_with([ChatMessage(role=Role.USER, content="q")]))

    assert response.message.tool_calls[0].name == "read_file"
    assert response.message.tool_calls[0].arguments_json == '{"path":"src/a.py"}'


async def test_an_error_response_is_raised_not_swallowed(provider, endpoint) -> None:
    endpoint.fail_status = 503

    with pytest.raises(OpenAICompatError, match="503"):
        await provider.complete(request_with([ChatMessage(role=Role.USER, content="q")]))


async def test_a_missing_key_fails_with_an_explanation(monkeypatch) -> None:
    monkeypatch.delenv("TEST_LLM_KEY", raising=False)
    root = create_root()
    fiber = await root.plugin(
        OpenAICompatLLMPlugin, {"base_url": "http://127.0.0.1:1/v1", "api_key_env": "TEST_LLM_KEY"}
    )
    try:
        with pytest.raises(OpenAICompatError, match="TEST_LLM_KEY is not set"):
            await root.llm.complete(request_with([]))
    finally:
        await dispose_fiber(fiber)


async def test_a_malformed_chunk_is_refused_rather_than_skipped(provider, endpoint) -> None:
    """A chunk we cannot read is a chunk the log cannot reproduce."""
    endpoint.events = [{"choices": [{"delta": {"content": "ok"}}]}]

    # Reach past the plugin to the parser, to assert the framing contract itself.
    async def lines():
        yield 'data: {"choices":[]}'
        yield "data: {broken"
        yield "data: [DONE]"

    with pytest.raises(OpenAICompatError):
        async for _ in iter_chunks(lines()):
            pass
