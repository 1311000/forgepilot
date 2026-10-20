"""The stdio transport against a **real** child process.

A fake in-process client would test the JSON-RPC shapes and nothing about the part that
actually breaks: pipes, a process that dies mid-call, a process that never answers, an
environment that leaks. So every test here writes a small server to ``tmp_path`` and
starts it — the same choice the worktree and GitHub tests make, for the same reason.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from forgepilot.providers.mcp.stdio import (
    ServerConfig,
    StdioMcpClient,
    parse_servers,
)
from forgepilot.seams.mcp import McpClient, McpRefused, McpTool, McpUnavailable
from forgepilot.seams.security import Operation

SERVER = textwrap.dedent(
    '''
    """A minimal MCP server: JSON-RPC over stdin/stdout, no dependencies."""
    import json
    import os
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    tools = [
        {"name": "echo", "description": "say it back", "inputSchema": {"type": "object"}},
        {"name": "fail", "description": "report an error result", "inputSchema": {}},
        {"name": "secret", "description": "not allowed by config", "inputSchema": {}},
        {"name": "env", "description": "the environment keys", "inputSchema": {}},
    ]

    if mode == "exit":
        sys.stderr.write("boom: this server refuses to start\\n")
        sys.exit(3)

    def send(payload):
        sys.stdout.write(json.dumps(payload) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/initialized":
            continue
        if mode == "silent" and method != "initialize":
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": request_id,
                  "result": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        elif method == "tools/list":
            if mode == "huge":
                send({"jsonrpc": "2.0", "id": request_id,
                      "result": {"tools": tools, "pad": "x" * 400000}})
                continue
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
        elif method == "tools/call":
            name = (message.get("params") or {}).get("name")
            arguments = (message.get("params") or {}).get("arguments") or {}
            if name == "echo":
                send({"jsonrpc": "2.0", "id": request_id,
                      "result": {"content": [{"type": "text", "text": arguments.get("say", "")}]}})
            elif name == "fail":
                send({"jsonrpc": "2.0", "id": request_id,
                      "result": {"content": [{"type": "text", "text": "it went wrong"}],
                                 "isError": True}})
            elif name == "env":
                send({"jsonrpc": "2.0", "id": request_id,
                      "result": {"content": [{"type": "text",
                                 "text": ",".join(sorted(os.environ))}]}})
            elif name == "secret":
                send({"jsonrpc": "2.0", "id": request_id,
                      "result": {"content": [{"type": "text", "text": "leaked"}]}})
            else:
                send({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32601, "message": "no such tool"}})
        else:
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32601, "message": "no such method"}})
    '''
)


@pytest.fixture()
def server(tmp_path: Path) -> Path:
    path = tmp_path / "server.py"
    path.write_text(SERVER, encoding="utf-8")
    return path


def _config(server: Path, *args: str, **overrides) -> ServerConfig:
    raw = {
        "name": "demo",
        "command": sys.executable,
        "args": [str(server), *args],
        "tools": ["echo", "fail", "env"],
        **overrides,
    }
    return ServerConfig.parse(raw, 0)


async def _client(server: Path, *args: str, **overrides) -> StdioMcpClient:
    return StdioMcpClient((_config(server, *args, **overrides),))


async def test_it_handshakes_and_lists_only_the_allowed_tools(server: Path, constraints_factory):
    client = await _client(server)
    try:
        tools = await client.tools(constraints_factory(Operation.CALL_EXTERNAL_TOOL))
    finally:
        await client.aclose()
    assert [tool.name for tool in tools] == ["echo", "fail", "env"], (
        "`secret` is listed by the server and not allowed by the configuration; a listing "
        "that shows it would invite a call that is refused for a reason the model cannot see"
    )
    assert tools[0].qualified == "demo/echo"
    assert tools[0].input_schema == {"type": "object"}


async def test_the_client_satisfies_the_seam_protocol(server: Path, constraints_factory):
    client = await _client(server)
    try:
        assert isinstance(client, McpClient)
    finally:
        await client.aclose()


async def test_calling_an_allowed_tool_returns_its_text(server: Path, constraints_factory):
    client = await _client(server)
    try:
        result = await client.call(
            McpTool("demo", "echo"),
            {"say": "hello"},
            constraints_factory(Operation.CALL_EXTERNAL_TOOL),
        )
    finally:
        await client.aclose()
    assert result.content == "hello"
    assert result.is_error is False


async def test_is_error_is_carried_rather_than_hidden(server: Path, constraints_factory):
    client = await _client(server)
    try:
        result = await client.call(
            McpTool("demo", "fail"), {}, constraints_factory(Operation.CALL_EXTERNAL_TOOL)
        )
    finally:
        await client.aclose()
    assert result.content == "it went wrong"
    assert result.is_error is True, (
        "MCP's own `isError` has to survive the transport: folding it into the text would "
        "log a failed tool call as a success"
    )


async def test_a_tool_outside_the_allowlist_is_refused_and_both_lists_are_named(
    server: Path, constraints_factory
):
    client = await _client(server)
    try:
        with pytest.raises(McpRefused) as refused:
            await client.call(
                McpTool("demo", "secret"), {}, constraints_factory(Operation.CALL_EXTERNAL_TOOL)
            )
    finally:
        await client.aclose()
    message = str(refused.value)
    assert "'secret' is not in the allowlist" in message
    assert "['echo', 'env', 'fail']" in message


async def test_an_unconfigured_server_is_refused_with_the_configured_names(
    server: Path, constraints_factory
):
    client = await _client(server)
    try:
        with pytest.raises(McpRefused) as refused:
            await client.call(
                McpTool("other", "echo"), {}, constraints_factory(Operation.CALL_EXTERNAL_TOOL)
            )
        assert "Configured: ['demo']" in str(refused.value)
    finally:
        await client.aclose()


async def test_a_server_that_dies_says_so_and_does_not_get_reused(
    server: Path, constraints_factory
):
    client = await _client(server, "exit")
    try:
        with pytest.raises(McpUnavailable) as broken:
            await client.tools(constraints_factory(Operation.CALL_EXTERNAL_TOOL))
    finally:
        await client.aclose()
    message = str(broken.value)
    assert "closed its output" in message or "exited with code 3" in message
    assert "boom: this server refuses to start" in message, (
        "the refusal has to carry the child's stderr: without it the operator sees "
        "'closed its output' and has nowhere to look"
    )


async def test_a_server_that_never_answers_times_out_and_is_killed(
    server: Path, constraints_factory
):
    client = await _client(server, "silent", timeout_s=0.5)
    try:
        with pytest.raises(McpUnavailable) as broken:
            await client.call(
                McpTool("demo", "echo"), {}, constraints_factory(Operation.CALL_EXTERNAL_TOOL)
            )
    finally:
        await client.aclose()
    assert "did not answer tools/call within 0.5s" in str(broken.value)


async def test_an_answer_over_the_limit_is_refused_and_the_process_killed(
    server: Path, constraints_factory
):
    client = await _client(server, "huge")
    try:
        with pytest.raises(McpUnavailable) as broken:
            await client.tools(constraints_factory(Operation.CALL_EXTERNAL_TOOL))
    finally:
        await client.aclose()
    assert "over the 256000-byte limit" in str(broken.value)


async def test_the_environment_is_an_allowlist_not_an_inheritance(
    server: Path, constraints_factory, monkeypatch
):
    monkeypatch.setenv("FORGEPILOT_SECRET_TOKEN", "do-not-leak")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    client = await _client(server, env_allow=["PATH", "SYSTEMROOT"])
    try:
        result = await client.call(
            McpTool("demo", "env"), {}, constraints_factory(Operation.CALL_EXTERNAL_TOOL)
        )
    finally:
        await client.aclose()
    seen = set(result.content.split(","))
    assert "PATH" in seen
    assert "FORGEPILOT_SECRET_TOKEN" not in seen, (
        "the child was handed the parent's whole environment, so every credential in it "
        "reached a third-party process"
    )


def test_the_plugin_validates_its_configuration():
    with pytest.raises(ValueError, match="has no `name`"):
        ServerConfig.parse({"command": "x", "tools": []}, 0)
    with pytest.raises(ValueError, match="has no `command`"):
        ServerConfig.parse({"name": "a", "tools": []}, 0)
    with pytest.raises(ValueError, match="has no `tools` list"):
        ServerConfig.parse({"name": "a", "command": "x"}, 0)
    with pytest.raises(ValueError, match="must be a list of names"):
        ServerConfig.parse({"name": "a", "command": "x", "tools": "echo"}, 0)
    with pytest.raises(ValueError, match="must be a list of strings"):
        ServerConfig.parse({"name": "a", "command": "x", "tools": [], "args": "-y"}, 0)
    with pytest.raises(ValueError, match="must be positive"):
        ServerConfig.parse({"name": "a", "command": "x", "tools": [], "timeout_s": 0}, 0)


def test_no_servers_key_is_an_empty_list_and_duplicates_are_refused():
    assert parse_servers(None) == ()
    with pytest.raises(ValueError, match="must be a list"):
        parse_servers({"demo": {"command": "x", "tools": []}})
    with pytest.raises(ValueError, match="two MCP servers are named 'a'"):
        parse_servers(
            [
                {"name": "a", "command": "x", "tools": []},
                {"name": "a", "command": "y", "tools": []},
            ]
        )


async def test_no_configured_server_means_no_tools_and_no_process(
    tmp_path: Path, constraints_factory
):
    client = StdioMcpClient(())
    try:
        assert await client.tools(constraints_factory(Operation.CALL_EXTERNAL_TOOL)) == ()
    finally:
        await client.aclose()


async def test_a_closed_client_refuses_rather_than_restarting(server: Path, constraints_factory):
    client = await _client(server)
    await client.aclose()
    with pytest.raises(McpUnavailable, match="closed"):
        await client.tools(constraints_factory(Operation.CALL_EXTERNAL_TOOL))


def test_the_bundle_ships_no_servers() -> None:
    """The strong default, read from the shipped file rather than asserted in prose."""
    import yaml

    forge = Path(__file__).resolve().parents[2]
    bundle = yaml.safe_load(
        (forge / "src" / "forgepilot" / "bundles" / "mcp" / "patch.yaml").read_text(
            encoding="utf-8"
        )
    )
    rows = {row["id"]: row for row in bundle}
    assert rows["mcp"]["config"]["servers"] == [], (
        "the MCP bundle ships servers; installing a bundle must not be the act that turns "
        "third-party processes on"
    )
    assert json.dumps(rows["toolsMcp"]["config"]) == "{}"
    assert (
        not (forge / "configs" / "profiles" / "default" / "profile.yaml")
        .read_text(encoding="utf-8")
        .count("mcp")
    ), "the default profile must not load the MCP bundle"
