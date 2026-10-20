"""The Anthropic provider: translation, the stream fold, and a real socket.

The transport half runs against **a real HTTP server speaking SSE**, not a mocked client,
for the same reason the OpenAI-compatible suite does: SSE framing and the two-event usage
split are the parts most likely to be wrong, and a mock would accept whatever framing this
test invented. What it still cannot test is a live endpoint's behaviour; there is no
Anthropic key on this machine, so **no live call was made** and the wire shapes are the
documented ones.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.providers.llm.anthropic import (
    AnthropicError,
    AnthropicLLMPlugin,
    chunk_from_event,
    chunks_from_events,
    to_payload,
)
from forgepilot.seams.llm import (
    ChatMessage,
    ChatRequest,
    Role,
    StreamChunk,
    ToolCallRequest,
    ToolSchema,
    Usage,
    drain_stream,
)


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
def test_system_sections_are_a_top_level_field() -> None:
    payload = to_payload(
        request_with([ChatMessage(role=Role.USER, content="hi")], system=("a", "b"))
    )

    assert payload["system"] == "a\n\nb"
    assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert payload["max_tokens"] == 256


def test_a_system_message_inside_the_turn_is_folded_up_and_not_left_as_a_role() -> None:
    payload = to_payload(
        request_with(
            [
                ChatMessage(role=Role.SYSTEM, content="mid-conversation rule"),
                ChatMessage(role=Role.USER, content="hi"),
            ],
            system=("top",),
        )
    )

    assert payload["system"] == "top\n\nmid-conversation rule"
    assert [message["role"] for message in payload["messages"]] == ["user"]


def test_an_assistant_turn_with_text_and_tools_is_one_message_of_blocks() -> None:
    payload = to_payload(
        request_with(
            [
                ChatMessage(
                    role=Role.ASSISTANT,
                    content="let me look",
                    tool_calls=(
                        ToolCallRequest(
                            call_id="toolu_1",
                            name="read_file",
                            arguments_json='{"path": "src/a.py"}',
                        ),
                    ),
                )
            ]
        )
    )

    assert payload["messages"] == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me look"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": "src/a.py"},
                },
            ],
        }
    ]


def test_tool_results_become_one_user_turn() -> None:
    payload = to_payload(
        request_with(
            [
                ChatMessage(role=Role.TOOL, content="first", tool_call_id="toolu_1"),
                ChatMessage(role=Role.TOOL, content="second", tool_call_id="toolu_2"),
                ChatMessage(role=Role.USER, content="and now?"),
            ]
        )
    )

    assert payload["messages"][0] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "first"},
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "second"},
        ],
    }, "two user turns in a row are refused by the API, so the run has to be folded"
    assert payload["messages"][1]["role"] == "user"


def test_a_tool_call_whose_arguments_do_not_parse_is_refused_by_name() -> None:
    with pytest.raises(AnthropicError, match="toolu_9"):
        to_payload(
            request_with(
                [
                    ChatMessage(
                        role=Role.ASSISTANT,
                        tool_calls=(
                            ToolCallRequest(
                                call_id="toolu_9", name="write_file", arguments_json="{nope"
                            ),
                        ),
                    )
                ]
            )
        )


def test_tools_are_declared_with_input_schema() -> None:
    payload = to_payload(
        request_with(
            [ChatMessage(role=Role.USER, content="hi")],
            tools=(ToolSchema(name="glob", description="find", parameters={"type": "object"}),),
        )
    )

    assert payload["tools"] == [
        {"name": "glob", "description": "find", "input_schema": {"type": "object"}}
    ]


# ---------------------------------------------------------------------------
# The stream fold
# ---------------------------------------------------------------------------
def test_usage_is_accumulated_across_the_two_events_that_carry_it() -> None:
    chunks = chunks_from_events(
        [
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 11, "cache_read_input_tokens": 7}},
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hi"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4},
            },
            {"type": "message_stop"},
        ]
    )

    response = drain_stream(chunks)
    assert response.message.content == "hi"
    assert response.usage == Usage(input_tokens=11, output_tokens=4, cache_read_tokens=7), (
        "input tokens arrive at message_start and output tokens at message_delta; "
        "drain_stream replaces usage wholesale, so either one alone loses the other"
    )


def test_tool_use_blocks_assemble_into_one_call_with_partial_json() -> None:
    chunks = chunks_from_events(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"path": '},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"a.py"}'},
            },
            {"type": "message_stop"},
        ]
    )

    response = drain_stream(chunks)
    assert [call.name for call in response.message.tool_calls] == ["read_file"]
    assert response.message.tool_calls[0].arguments_json == '{"path": "a.py"}'
    assert response.message.tool_calls[0].call_id == "toolu_1"


def test_an_unknown_event_is_not_an_error_and_thinking_is_carried_as_reasoning() -> None:
    state_chunks = chunks_from_events(
        [
            {"type": "ping"},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "hmm"},
            },
        ]
    )

    assert StreamChunk(kind="reasoning", text="hmm") in state_chunks
    assert chunk_from_event({"type": "future_thing"}, _state()) == []


def _state():
    from forgepilot.providers.llm.anthropic import _StreamState

    return _StreamState()


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
            payload = b'{"error": {"type": "invalid_request_error"}}'
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
            frame = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
            self.wfile.write(f"{len(frame):X}\r\n".encode())
            self.wfile.write(frame)
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
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "secret-value")
    root = create_root()
    fiber = await root.plugin(
        AnthropicLLMPlugin,
        {
            "model": "test-model",
            "base_url": f"http://127.0.0.1:{endpoint.server_port}/v1",
            "api_key_env": "TEST_ANTHROPIC_KEY",
            "price_per_1k_input": 3.0,
            "price_per_1k_output": 15.0,
        },
    )
    try:
        yield root.llm
    finally:
        await dispose_fiber(fiber)


async def test_the_provider_streams_a_reply_and_prices_it(provider, endpoint) -> None:
    endpoint.events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 100}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "the answer"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 10},
        },
        {"type": "message_stop"},
    ]

    response = await provider.complete(request_with([ChatMessage(role=Role.USER, content="q")]))

    assert response.message.content == "the answer"
    assert response.usage == Usage(
        input_tokens=100, output_tokens=10, cost_usd=pytest.approx(0.45)
    ), "the price belongs to the provider: it is the only object that knows the model"
    assert endpoint.requests[0][0] == "/v1/messages"


async def test_the_provider_sends_the_key_by_header_and_the_pinned_version(
    provider, endpoint
) -> None:
    endpoint.events = [{"type": "message_stop"}]

    await provider.complete(request_with([ChatMessage(role=Role.USER, content="q")]))

    headers = {name.lower(): value for name, value in endpoint.requests[0][2].items()}
    assert headers["x-api-key"] == "secret-value"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in headers, (
        "the header name is part of the wire format: a bearer token here is a 401 that "
        "reads like a bad key"
    )


async def test_an_error_response_carries_the_status_and_the_body(provider, endpoint) -> None:
    endpoint.fail_status = 400

    with pytest.raises(AnthropicError, match="400"):
        await provider.complete(request_with([ChatMessage(role=Role.USER, content="q")]))


async def test_a_missing_key_names_the_variable_rather_than_the_value(monkeypatch) -> None:
    monkeypatch.delenv("TEST_ANTHROPIC_KEY", raising=False)
    root = create_root()
    fiber = await root.plugin(
        AnthropicLLMPlugin,
        {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "TEST_ANTHROPIC_KEY"},
    )
    try:
        with pytest.raises(AnthropicError, match="TEST_ANTHROPIC_KEY is not set"):
            await root.llm.complete(request_with([ChatMessage(role=Role.USER, content="q")]))
    finally:
        await dispose_fiber(fiber)
