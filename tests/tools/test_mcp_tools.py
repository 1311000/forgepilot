"""The two tools that reach an MCP server: what they show, and what they refuse.

The seam's job is to keep "the server has no such tool" and "the server is not
answering" apart; these tests are where that separation has to survive the trip
through a tool result. The rest is about the allowlist being enforced *here* --
a tool the model can see and cannot call is a policy, and a tool that is called
because the server listed it is a hole.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from forgepilot.runtime.tools.mcp import (
    BUILT_NAMES,
    MAX_LISTED,
    build_mcp_definitions,
    built_names,
)
from forgepilot.seams.mcp import McpCallResult, McpRefused, McpTool, McpUnavailable
from forgepilot.seams.security import ExecutionConstraints, Operation
from forgepilot.seams.tools import (
    Capabilities,
    ToolCall,
    ToolEffect,
)
from tests.conftest import constraints_for

ECHO = McpTool(
    server="demo",
    name="echo",
    description="echo the text back",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
)
FAIL = McpTool(server="demo", name="fail", description="report an error")

ALLOWED = (Operation.CALL_EXTERNAL_TOOL,)


class FakeClient:
    """A client whose answers are values, so a refusal cannot be mistaken for a call."""

    def __init__(
        self,
        *,
        tools: tuple[McpTool, ...] = (ECHO, FAIL),
        listing_error: Exception | None = None,
        call_error: Exception | None = None,
        result: McpCallResult | None = None,
    ) -> None:
        self._tools = tools
        self._listing_error = listing_error
        self._call_error = call_error
        self._result = result or McpCallResult(content="echo: hi")
        self.listed = 0
        self.called: list[tuple[McpTool, Mapping[str, Any]]] = []

    async def tools(self, constraints: ExecutionConstraints) -> tuple[McpTool, ...]:
        self.listed += 1
        if self._listing_error is not None:
            raise self._listing_error
        return self._tools

    async def call(
        self,
        tool: McpTool,
        arguments: Mapping[str, Any],
        constraints: ExecutionConstraints,
    ) -> McpCallResult:
        self.called.append((tool, dict(arguments)))
        if self._call_error is not None:
            raise self._call_error
        return self._result

    async def aclose(self) -> None:
        return None


def _definitions(client: FakeClient) -> dict[str, Any]:
    return {definition.name: definition for definition in build_mcp_definitions(client)}


def _ctx(*operations: Operation) -> Any:
    # `Capabilities()` is deliberately empty: an external tool is granted no host
    # capability, so a test that gave it one would be testing a deployment that cannot
    # exist.
    return SimpleNamespace(
        capabilities=Capabilities(),
        constraints=constraints_for(*(operations or ALLOWED)),
    )


def _call(name: str = "mcp_servers", **arguments: Any) -> ToolCall:
    return ToolCall(call_id="c-mcp", name=name, arguments_json=json.dumps(arguments))


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------
async def test_the_listing_carries_each_tools_own_schema() -> None:
    client = FakeClient()
    outcome = await _definitions(client)["mcp_servers"].execute(_call(), _ctx())

    assert not outcome.is_error, outcome.content
    assert "demo/echo" in outcome.content
    assert "echo the text back" in outcome.content
    assert '"properties"' in outcome.content, (
        "the server wrote the schema; a listing without it makes the model guess at "
        "arguments the server never promised to accept"
    )
    assert outcome.meta["tools"] == 2
    assert outcome.meta["servers"] == ["demo"]


async def test_an_empty_listing_says_why_it_is_empty() -> None:
    outcome = await _definitions(FakeClient(tools=()))["mcp_servers"].execute(_call(), _ctx())

    assert not outcome.is_error
    assert "no MCP tools are configured" in outcome.content
    assert "bundle ships with no servers" in outcome.content
    assert "empty `tools` allowlist" in outcome.content


async def test_a_catalogue_over_the_cap_names_what_it_did_not_show() -> None:
    many = tuple(McpTool(server="demo", name=f"tool-{index}") for index in range(MAX_LISTED + 3))
    outcome = await _definitions(FakeClient(tools=many))["mcp_servers"].execute(_call(), _ctx())

    assert "and 3 more, not listed" in outcome.content
    assert outcome.content.count("schema:") == MAX_LISTED


# ---------------------------------------------------------------------------
# Refusals: three different answers, and none of them may look like another
# ---------------------------------------------------------------------------
async def test_a_tool_that_is_not_allowed_is_refused_by_the_client_not_as_an_outage() -> None:
    client = FakeClient(listing_error=McpRefused("'secret' is not in the allowlist"))
    outcome = await _definitions(client)["mcp_servers"].execute(_call(), _ctx())

    assert outcome.is_error
    assert outcome.meta["phase"] == "refused"
    assert "secret" in outcome.content


async def test_a_server_that_is_down_is_a_transport_error_not_a_missing_tool() -> None:
    client = FakeClient(call_error=McpUnavailable("exited with code 3"))
    definitions = _definitions(client)
    outcome = await definitions["mcp_call"].execute(
        _call("mcp_call", server="demo", tool="echo"), _ctx()
    )

    assert outcome.is_error
    assert outcome.meta["phase"] == "transport", (
        "a model that reads an outage as 'no such tool' goes looking for a name that was right"
    )
    assert "did not answer" in outcome.content
    assert client.called, "the call reached the client and the outage came back"


@pytest.mark.parametrize("name", ["mcp_servers", "mcp_call"])
async def test_the_operation_is_required_before_anything_reaches_the_client(name) -> None:
    client = FakeClient()
    outcome = await _definitions(client)[name].execute(_call(name), _ctx(Operation.READ_CODE))

    assert outcome.is_error
    assert outcome.meta["phase"] == "refused"
    assert "call_external_tool" in outcome.content
    assert (client.listed, client.called) == (0, []), (
        "the policy is checked before the transport: a refused call must not start a "
        "process, discover a tool, or touch a server"
    )


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------
async def test_a_call_returns_the_servers_text_and_its_own_error_flag() -> None:
    client = FakeClient(result=McpCallResult(content="it failed", is_error=True))
    outcome = await _definitions(client)["mcp_call"].execute(
        _call("mcp_call", server="demo", tool="fail", arguments={"text": "x"}), _ctx()
    )

    assert outcome.content == "it failed"
    assert outcome.is_error is True, (
        "MCP's isError is carried, not rewritten: a result that says 'failed' in prose "
        "while the pipeline records a success is the lie this repository keeps finding"
    )
    assert outcome.meta["isError"] is True
    assert client.called and client.called[0][0].qualified == "demo/fail"
    assert client.called[0][1] == {"text": "x"}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "needs `server` and `tool`"),
        ({"server": "demo"}, "needs `server` and `tool`"),
        ({"server": "demo", "tool": "echo", "arguments": "not an object"}, "must be an object"),
    ],
)
async def test_arguments_that_cannot_name_a_tool_are_refused_locally(arguments, expected) -> None:
    client = FakeClient()
    call = ToolCall(call_id="c-mcp", name="mcp_call", arguments_json=json.dumps(arguments))
    outcome = await _definitions(client)["mcp_call"].execute(call, _ctx())

    assert outcome.is_error
    assert outcome.meta["phase"] == "arguments"
    assert expected in outcome.content
    assert client.called == []


async def test_arguments_that_are_not_json_are_refused_locally() -> None:
    client = FakeClient()
    call = ToolCall(call_id="c-mcp", name="mcp_call", arguments_json="{nope")
    outcome = await _definitions(client)["mcp_call"].execute(call, _ctx())

    assert outcome.is_error
    assert outcome.meta["phase"] == "arguments"
    assert "not JSON" in outcome.content
    assert client.called == []


# ---------------------------------------------------------------------------
# The declarations
# ---------------------------------------------------------------------------
def test_both_tools_are_external_and_need_the_operation() -> None:
    definitions = build_mcp_definitions(FakeClient())

    assert {definition.name for definition in definitions} == set(BUILT_NAMES)
    assert built_names() == BUILT_NAMES
    for definition in definitions:
        assert definition.effects == frozenset({ToolEffect.EXTERNAL}), (
            "an external tool is not this deployment's read: claiming READ for it would "
            "hand the model a capability it did not earn"
        )
        assert definition.required_operation is Operation.CALL_EXTERNAL_TOOL
