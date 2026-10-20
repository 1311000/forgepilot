"""The two tools that reach an MCP server, and the rules they carry.

An MCP server's tools are not registered as ForgePilot tools, and that is a decision
rather than an omission: registration happens when a plugin loads, the tool list belongs
to a process that may be down, slow, or different from the one that answered at boot. So
the surface is two tools whose arguments name what to reach:

- ``mcp_servers`` lists what the configured servers offer **and the schema of each**, so
  the model reads the server's own contract instead of this codebase's guess at it;
- ``mcp_call`` invokes one, by ``server`` and ``tool``, with an arguments object.

Three consequences worth stating, because each one is a way this could be wrong:

1. **The allowlist is enforced at call time, by name.** A tool that exists on the server
   but is not in the configuration's `tools` list is refused with both lists printed,
   so a typo in the call and a missing entry in the configuration cannot be confused.
2. **The effect is ``EXTERNAL``, which grants no host capability.** A remote tool is not
   this deployment's file read or shell: it runs inside a process the pipeline does not
   mediate, so claiming ``READ`` for it would hand the model a capability it did not
   earn. What bounds it is the allowlist and the operation — never the effect.
3. **The operation is ``call_external_tool``.** Like ``fetch_url``, it leaves the code
   this deployment owns. A deployment that has not granted it can be shown the tool and
   still not call it, which is the point of a policy.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from forgepilot.seams.mcp import (
    McpCallResult,
    McpClient,
    McpRefused,
    McpTool,
    McpUnavailable,
)
from forgepilot.seams.security import Operation
from forgepilot.seams.tools import (
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolOutcome,
)

SERVICE_NAME = "toolsMcp"

#: How many tools one listing may show. A server with hundreds of tools is a server whose
#: configuration should name the few this deployment wants; the cap makes that visible
#: instead of spending the model's context on the whole catalogue.
MAX_LISTED = 200

#: A single schema is printed in full unless it is enormous; a truncated schema is worse
#: than a name and a pointer, so the listing says when it clipped and says how to see it.
MAX_SCHEMA_CHARS = 4000

#: The names :func:`build_mcp_definitions` produces. Shared rather than re-listed, so the
#: contract test that asks "can a project name this tool?" cannot drift from the builder.
BUILT_NAMES = frozenset({"mcp_servers", "mcp_call"})


def built_names() -> frozenset[str]:
    """What this module registers, without a client — the builder needs one to close over.

    A tool's *name* is a fact about the builder; its *body* needs a transport. Callers that
    only need to know what exists (the contract tests) use this.
    """
    return BUILT_NAMES


def _refusal(message: str, *, meta: Mapping[str, Any] | None = None) -> ToolOutcome:
    return ToolOutcome(
        content=message, is_error=True, meta={"phase": "refused", **dict(meta or {})}
    )


def _bad_arguments(message: str) -> ToolOutcome:
    return ToolOutcome(content=message, is_error=True, meta={"phase": "arguments"})


def _schema_of(tool: McpTool) -> str:
    try:
        text = json.dumps(dict(tool.input_schema), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - a schema that cannot round-trip
        return "(the server's schema is not JSON)"
    if len(text) <= MAX_SCHEMA_CHARS:
        return text
    return text[:MAX_SCHEMA_CHARS] + f"… [schema truncated at {MAX_SCHEMA_CHARS} characters]"


def _render_listing(tools: tuple[McpTool, ...]) -> str:
    if not tools:
        return (
            "no MCP tools are configured. The `mcp` bundle ships with no servers, and an "
            "empty `tools` allowlist refuses every tool: a deployment names the servers and "
            "the tools it wants in `mcp.servers`."
        )
    shown = tools[:MAX_LISTED]
    lines = [f"{len(tools)} MCP tool(s) available:"]
    for tool in shown:
        description = f" — {tool.description.strip()}" if tool.description.strip() else ""
        lines.append(f"- {tool.qualified}{description}")
        lines.append(f"  schema: {_schema_of(tool)}")
    if len(tools) > len(shown):
        lines.append(
            f"… and {len(tools) - len(shown)} more, not listed. Call `mcp_call` with the "
            f"server and tool names from the configuration; the cap is {MAX_LISTED}."
        )
    return "\n".join(lines)


def build_mcp_definitions(client: McpClient) -> tuple[ToolDefinition, ...]:
    """The two tools, closed over one client.

    Closed over, not looked up: a tool body is handed a :class:`ToolContext` and no
    service container, so the client has to arrive at build time — the same shape the
    ``web_fetch`` transport uses, and the reason a tool cannot reach a capability its
    declaration did not earn.
    """

    async def list_tools(call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        if Operation.CALL_EXTERNAL_TOOL not in ctx.constraints.permitted_operations:
            return _refusal(
                "this run may not call external tools (the call_external_tool operation is "
                "not permitted); use the tools that stay inside the workspace"
            )
        try:
            tools = await client.tools(ctx.constraints)
        except McpRefused as exc:
            return _refusal(str(exc))
        except McpUnavailable as exc:
            return ToolOutcome(
                content=f"an MCP server is not answering: {exc}",
                is_error=True,
                meta={"phase": "transport"},
            )
        return ToolOutcome(
            content=_render_listing(tools),
            meta={"tools": len(tools), "servers": sorted({tool.server for tool in tools})},
        )

    async def call_tool(call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        if Operation.CALL_EXTERNAL_TOOL not in ctx.constraints.permitted_operations:
            return _refusal(
                "this run may not call external tools (the call_external_tool operation is "
                "not permitted); use the tools that stay inside the workspace"
            )
        try:
            arguments = json.loads(call.arguments_json or "{}")
        except json.JSONDecodeError as exc:
            return _bad_arguments(f"the arguments are not JSON: {exc}")
        if not isinstance(arguments, dict):
            return _bad_arguments("the arguments must be an object with `server` and `tool`")

        server = str(arguments.get("server") or "").strip()
        tool_name = str(arguments.get("tool") or "").strip()
        if not server or not tool_name:
            return _bad_arguments(
                "mcp_call needs `server` and `tool` (call `mcp_servers` to see both)"
            )
        payload = arguments.get("arguments") or {}
        if not isinstance(payload, Mapping):
            return _bad_arguments("`arguments` must be an object, not a string or a list")

        tool = McpTool(server=server, name=tool_name)
        try:
            result: McpCallResult = await client.call(tool, payload, ctx.constraints)
        except McpRefused as exc:
            return _refusal(str(exc), meta={"server": server, "tool": tool_name})
        except McpUnavailable as exc:
            return ToolOutcome(
                content=f"the MCP server {server!r} did not answer: {exc}",
                is_error=True,
                meta={"phase": "transport", "server": server, "tool": tool_name},
            )
        return ToolOutcome(
            content=result.content,
            is_error=result.is_error,
            meta={
                "server": server,
                "tool": tool_name,
                "isError": result.is_error,
                "chars": len(result.content),
            },
        )

    return (
        ToolDefinition(
            name="mcp_servers",
            description=(
                "List the tools the configured MCP servers offer, with each one's JSON "
                "schema so the arguments can be built correctly. Empty is a real answer: "
                "the deployment configured no servers or allowed no tools."
            ),
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            effects=frozenset({ToolEffect.EXTERNAL}),
            execute=list_tools,
            required_operation=Operation.CALL_EXTERNAL_TOOL,
        ),
        ToolDefinition(
            name="mcp_call",
            description=(
                "Invoke one tool on one configured MCP server. Pass `server` and `tool` "
                "exactly as `mcp_servers` printed them, and `arguments` as the object that "
                "tool's schema describes. The server runs as this deployment's process, "
                "outside the workspace sandbox."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "Configured server name."},
                    "tool": {"type": "string", "description": "Tool name as listed."},
                    "arguments": {
                        "type": "object",
                        "description": "The tool's arguments, per its schema.",
                    },
                },
                "required": ["server", "tool"],
                "additionalProperties": False,
            },
            effects=frozenset({ToolEffect.EXTERNAL}),
            execute=call_tool,
            required_operation=Operation.CALL_EXTERNAL_TOOL,
        ),
    )


class McpToolsPlugin:
    """Registers the two MCP tools when a registry exists.

    Loaded only by ``bundles/mcp``, whose ``mcp`` row configures no servers. Loading it
    without configuring a server therefore registers two tools that answer "nothing is
    configured" — honest and visible, exactly like an empty ``web_fetch`` allowlist.
    """

    inject = ["tools", "mcp"]

    def __init__(self, ctx: Any, config: Any = None, **options: Any) -> None:
        self.ctx = ctx
        self.config = dict(config or {})
        self.options = options

    def init(self) -> Any:
        registry = self.ctx.tools
        client = self.ctx.mcp
        unregisters = [
            registry.register(definition) for definition in build_mcp_definitions(client)
        ]

        def cleanup() -> None:
            for unregister in unregisters:
                unregister()

        return cleanup
