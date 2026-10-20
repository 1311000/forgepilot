"""A tool that lives in another process, reached without trusting that process.

The gap this seam closes is the one an agent platform cannot invent away: the tools a
deployment needs are not all written by the deployment. An MCP server brings its own
tools, its own schemas and its own failures, and the only two honest choices are to
integrate it or to leave the capability out. Integrating it must not hand it authority,
so the rules are stated here rather than in whichever provider arrives first:

1. **A server's claim is not a permission.** The server says what a tool does in its
   description; the *host* decides what the call is allowed to be, and the model's call
   is authorised by this deployment's constraints exactly like a local tool's. A server
   that describes itself as "read-only" changes nothing about that.
2. **Nothing is reachable until a configuration names it.** The shipped bundle lists no
   servers at all, and an empty tool allowlist refuses every tool by name. A default that
   becomes permissive when a bundle is installed is not a default.
3. **"The server has no such tool" and "the server is not answering" are different
   answers.** The first is a mistake in the call and the model can fix it; the second is
   an outage, and a tool that reports it as "no such tool" sends the model looking for a
   name that was right. Every refusal here says which of the two it is.
4. **The schema is passed through unchanged.** The server wrote it; rewriting it would
   make the tool's contract this codebase's guess, and the model would produce arguments
   the server never promised to accept.

What this seam deliberately does **not** offer is a sandbox for the server. An MCP server
is a process this deployment starts, and it can do whatever that process can — so the
decision to run one belongs with `run` and `web_fetch`, in configuration, and a
deployment that cannot audit a server should not configure it. The transport bounds what
comes *back* (size, time), and the pipeline logs every call as a tool result; neither is
a containment boundary, and calling it one would be the defect this repository keeps
finding.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from forgepilot.seams.security import ExecutionConstraints

SERVICE_NAME = "mcp"

__all__ = [
    "McpCallResult",
    "McpClient",
    "McpRefused",
    "McpTool",
    "McpUnavailable",
    "SERVICE_NAME",
]


class McpRefused(PermissionError):
    """The call is refused by name: not configured, or not in the allowlist."""


class McpUnavailable(RuntimeError):
    """The server could not be reached, or broke the protocol, or timed out."""


@dataclass(frozen=True, slots=True)
class McpTool:
    """One tool as its server declared it.

    ``input_schema`` is the server's JSON Schema, kept verbatim (rule 4). ``server`` is
    the configuration's name for the process, not the server's own idea of its name:
    two deployments that run the same server under different names must be able to tell
    their calls apart in the log.
    """

    server: str
    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        """``<server>/<tool>`` — what the model writes and what the log records."""
        return f"{self.server}/{self.name}"


@dataclass(frozen=True, slots=True)
class McpCallResult:
    """What a call returned, already rendered to text by the transport.

    ``is_error`` is the server's own report (MCP's ``isError``) and is carried rather
    than folded into the text: a tool result that says "failed" in prose while the
    pipeline records a success is the same lie as an untruthful PR body.
    """

    content: str
    is_error: bool = False


@runtime_checkable
class McpClient(Protocol):
    """The seam a deployment implements to reach a server — or a facade over several.

    One provider serves all configured servers rather than one provider per server, for
    the same reason the retriever seam has one provider with many roots: the number of
    servers is configuration, and a plugin row per server would make the plugin tree grow
    with a file.
    """

    async def tools(self, constraints: ExecutionConstraints) -> tuple[McpTool, ...]:
        """Every tool the allowlist permits, from every configured server.

        Discovery is part of this call rather than of plugin loading: a server that is
        down at boot must not stop the process from starting, and a tool list that
        changes while the process runs must be visible to the next call.
        """
        ...

    async def call(
        self,
        tool: McpTool,
        arguments: Mapping[str, Any],
        constraints: ExecutionConstraints,
    ) -> McpCallResult:
        """Invoke one allowed tool, or raise :class:`McpRefused`/:class:`McpUnavailable`."""
        ...

    async def aclose(self) -> None:
        """Stop every server this client started. Idempotent."""
        ...
