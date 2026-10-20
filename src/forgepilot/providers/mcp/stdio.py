"""MCP over stdio: one child process per configured server, JSON-RPC on its pipes.

The transport is the least interesting part of this file; the interesting part is what
it refuses. A server is started only when something asks it for its tool list, its
standard input and output are the only channels, its environment is an explicit
allowlist of *names*, and every response is bounded in size and in time. A server that
wedges is killed and reported as unavailable — it is never reused, because the next
request would be answered by a process whose state nobody can reason about.

Config::

    servers:
      - name: "filesystem"                 # required, and this is the name in the log
        command: "npx"                     # required, executed without a shell
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/srv/repo"]
        tools: ["read_file", "list_directory"]   # required; empty refuses everything
        env_allow: ["PATH", "SYSTEMROOT"]  # optional, names only
        timeout_s: 30                      # optional, per request

Two things are deliberately awkward here:

- **`tools` has no "all" form.** An allowlist that can be spelled ``"*"`` is one typo
  away from every tool a server ever adds, including ones that did not exist when the
  configuration was reviewed. The deployment names what it wants.
- **The command is not a shell line.** ``command`` plus ``args`` goes to
  ``create_subprocess_exec``, so a quoting mistake fails loudly instead of being
  interpreted by ``cmd.exe`` — the same reason ``run_checks`` is argv-only.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.mcp import (
    SERVICE_NAME,
    McpCallResult,
    McpRefused,
    McpTool,
    McpUnavailable,
)
from forgepilot.seams.security import ExecutionConstraints

#: Enough for a Python or Node child to start on both platforms, and nothing that could
#: carry a credential by accident. A server that needs more names them in ``env_allow``.
_DEFAULT_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")

#: Bumped only when this client's own behaviour is what changed; the protocol version is
#: negotiated in ``initialize`` and the server may answer with an older one.
_CLIENT_INFO = {"name": "forgepilot", "version": "0.0.1"}
_PROTOCOL_VERSION = "2025-06-18"

#: A single JSON-RPC response larger than this is refused rather than parsed. A server
#: that answers with a whole repository has misunderstood the call, and reading it would
#: spend the model's context on the mistake.
_DEFAULT_MAX_BYTES = 256_000

#: What comes back when a tool returns blocks that are not text. Naming the type is
#: honest; rendering a PNG as mojibake is not, and dropping it silently is worse.
_NON_TEXT = "[{kind} content, {size} bytes, not rendered]"


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """One configured server, validated at load time rather than at first call."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    tools: frozenset[str] = frozenset()
    env_allow: tuple[str, ...] = _DEFAULT_ENV
    timeout_s: float = 30.0

    @classmethod
    def parse(cls, raw: Mapping[str, Any], index: int) -> ServerConfig:
        where = f"mcp.servers[{index}]"
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"{where} has no `name`; the name is what the log records")
        command = str(raw.get("command") or "").strip()
        if not command:
            raise ValueError(f"{where} (`{name}`) has no `command`")
        args_raw = raw.get("args") or []
        if isinstance(args_raw, str) or not isinstance(args_raw, (list, tuple)):
            raise ValueError(f"{where}.args must be a list of strings, not {args_raw!r}")
        tools_raw = raw.get("tools")
        if tools_raw is None:
            raise ValueError(
                f"{where} (`{name}`) has no `tools` list. Empty refuses every tool, and "
                f"a missing key would refuse them silently — say which it is."
            )
        if isinstance(tools_raw, str) or not isinstance(tools_raw, (list, tuple)):
            raise ValueError(f"{where}.tools must be a list of names, not {tools_raw!r}")
        env_raw = raw.get("env_allow")
        if env_raw is not None and (
            isinstance(env_raw, str) or not isinstance(env_raw, (list, tuple))
        ):
            raise ValueError(f"{where}.env_allow must be a list of names, not {env_raw!r}")
        timeout = float(raw.get("timeout_s", 30.0))
        if timeout <= 0:
            raise ValueError(f"{where}.timeout_s must be positive, not {timeout}")
        return cls(
            name=name,
            command=command,
            args=tuple(str(a) for a in args_raw),
            tools=frozenset(str(t) for t in tools_raw),
            env_allow=tuple(str(n) for n in (env_raw if env_raw is not None else _DEFAULT_ENV)),
            timeout_s=timeout,
        )


@dataclass
class _Connection:
    """A started server: the process, its pending reader and the request counter."""

    config: ServerConfig
    process: Any = None
    stderr: list[str] = field(default_factory=list)
    next_id: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def parse_servers(raw: Any) -> tuple[ServerConfig, ...]:
    """Validate the ``servers`` list, refusing a shape that would lose an order or a name.

    Separate from the plugin so it can be tested without a kernel, and separate from
    :meth:`ServerConfig.parse` because these two failures are about the *list*: a mapping
    loses the order the deployment wrote, and a duplicate name makes two processes
    indistinguishable in the log and in a refusal message.
    """
    if raw is None:
        return ()
    if isinstance(raw, Mapping) or isinstance(raw, str):
        raise ValueError(
            "mcp.servers must be a list; a mapping would lose the order the deployment "
            "wrote and the duplicate-name check with it"
        )
    servers = tuple(ServerConfig.parse(entry, index) for index, entry in enumerate(raw))
    seen: set[str] = set()
    for server in servers:
        if server.name in seen:
            raise ValueError(
                f"two MCP servers are named {server.name!r}; the name is how a call is "
                f"addressed and how the log is read, so it must be unique"
            )
        seen.add(server.name)
    return servers


class StdioMcpClient:
    """Every configured server, started lazily and killed on close.

    One object for all servers because the number of servers is configuration: a provider
    row per server would make the plugin tree grow with a file, and the seam already
    returns the union of their tools.
    """

    def __init__(
        self,
        servers: tuple[ServerConfig, ...] = (),
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_chars: int = 60_000,
    ) -> None:
        self._servers = {server.name: server for server in servers}
        self._connections: dict[str, _Connection] = {}
        self._max_bytes = max_bytes
        self._max_chars = max_chars
        self._closed = False
        self._atexit_registered = False

    # -- lifecycle ---------------------------------------------------------

    async def _connection(self, name: str) -> _Connection:
        if self._closed:
            raise McpUnavailable(f"MCP client is closed; cannot reach {name!r}")
        config = self._servers.get(name)
        if config is None:
            raise McpRefused(
                f"no MCP server named {name!r} is configured. Configured: "
                f"{sorted(self._servers) or 'none'}"
            )
        existing = self._connections.get(name)
        if existing is not None and existing.process is not None:
            if existing.process.returncode is None:
                return existing
            # A process that has exited is not restarted silently: the request that
            # discovered it is the one that gets to report the outage.
            raise McpUnavailable(
                f"MCP server {name!r} exited with code {existing.process.returncode}; its "
                f"stderr said: {self._stderr_tail(existing)}"
            )
        connection = existing or _Connection(config=config)
        self._connections[name] = connection
        await self._start(connection)
        return connection

    async def _start(self, connection: _Connection) -> None:
        config = connection.config
        env = {name: os.environ[name] for name in config.env_allow if name in os.environ}
        try:
            connection.process = await asyncio.create_subprocess_exec(
                config.command,
                *config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # asyncio's reader refuses a line over its own limit *before* this module
                # can compare it with `max_bytes`, and that refusal is a bare ValueError
                # naming a number the operator never configured. Raising the reader's
                # limit above the configured cap means the cap is the first thing that
                # bites, so the refusal names the number the deployment actually set.
                limit=self._max_bytes + 1024,
            )
        except (OSError, ValueError) as exc:
            raise McpUnavailable(
                f"MCP server {config.name!r} could not be started ({config.command}): {exc}"
            ) from exc
        self._register_atexit()
        try:
            await self._request(
                connection,
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
            )
            await self._notify(connection, "notifications/initialized", {})
        except McpUnavailable:
            await self._kill(connection)
            raise

    def _register_atexit(self) -> None:
        # The supported path is `aclose()`. This is the backstop for a crash or a
        # shutdown that skipped it: a process this deployment started must not outlive
        # it, and on Windows an orphaned child keeps running after the parent exits.
        if not self._atexit_registered:
            atexit.register(self._kill_all_sync)
            self._atexit_registered = True

    async def aclose(self) -> None:
        self._closed = True
        for connection in list(self._connections.values()):
            await self._kill(connection)
        self._connections.clear()

    def _kill_all_sync(self) -> None:
        for connection in list(self._connections.values()):
            process = connection.process
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:  # pragma: no cover - already gone
                    pass

    async def _kill(self, connection: _Connection) -> None:
        process = connection.process
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:  # pragma: no cover - already gone
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:  # pragma: no cover - already gone
                    pass
                await process.wait()
        connection.process = None

    # -- protocol ----------------------------------------------------------

    async def _send(self, connection: _Connection, payload: Mapping[str, Any]) -> None:
        process = connection.process
        if process is None or process.stdin is None:
            raise McpUnavailable(f"MCP server {connection.config.name!r} has no stdin")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            process.stdin.write(line.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise McpUnavailable(
                f"MCP server {connection.config.name!r} closed its input: {exc}"
            ) from exc

    async def _notify(
        self, connection: _Connection, method: str, params: Mapping[str, Any]
    ) -> None:
        await self._send(connection, {"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(
        self, connection: _Connection, method: str, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        async with connection.lock:
            connection.next_id += 1
            request_id = connection.next_id
            await self._send(
                connection,
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            )
            try:
                message = await asyncio.wait_for(
                    self._read(connection), timeout=connection.config.timeout_s
                )
            except TimeoutError as exc:
                await self._kill(connection)
                raise McpUnavailable(
                    f"MCP server {connection.config.name!r} did not answer {method} within "
                    f"{connection.config.timeout_s:g}s; the process was killed rather than "
                    f"reused, because its state is unknown"
                ) from exc
        if message.get("id") != request_id:
            raise McpUnavailable(
                f"MCP server {connection.config.name!r} answered id {message.get('id')!r} "
                f"to request {request_id}; responses and requests are paired by id"
            )
        error = message.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, Mapping) else None
            text = error.get("message") if isinstance(error, Mapping) else error
            raise McpUnavailable(
                f"MCP server {connection.config.name!r} refused {method} ({code}): {text}"
            )
        result = message.get("result")
        if not isinstance(result, Mapping):
            raise McpUnavailable(
                f"MCP server {connection.config.name!r} answered {method} without a result "
                f"object: {str(result)[:200]}"
            )
        return result

    async def _read(self, connection: _Connection) -> Mapping[str, Any]:
        process = connection.process
        if process is None or process.stdout is None:
            raise McpUnavailable(f"MCP server {connection.config.name!r} has no stdout")
        try:
            line = await process.stdout.readline()
        except ValueError as exc:
            # Belt and braces: `limit` above is set past `max_bytes`, so this should be
            # unreachable. If a platform ignores it, the refusal still names the cap
            # rather than leaking asyncio's internal number.
            await self._kill(connection)
            raise McpUnavailable(
                f"MCP server {connection.config.name!r} answered with a line over the "
                f"{self._max_bytes}-byte limit; the process was killed ({exc})"
            ) from exc
        if not line:
            raise McpUnavailable(
                f"MCP server {connection.config.name!r} closed its output; stderr said: "
                f"{await self._drain_stderr(connection)}"
            )
        if len(line) > self._max_bytes:
            await self._kill(connection)
            raise McpUnavailable(
                f"MCP server {connection.config.name!r} answered with {len(line)} bytes, "
                f"over the {self._max_bytes}-byte limit; the process was killed"
            )
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpUnavailable(
                f"MCP server {connection.config.name!r} wrote a line that is not JSON "
                f"({exc}): {line[:200]!r}"
            ) from exc
        if not isinstance(message, Mapping):
            raise McpUnavailable(
                f"MCP server {connection.config.name!r} wrote {type(message).__name__} "
                f"where an object was expected"
            )
        return message

    async def _drain_stderr(self, connection: _Connection) -> str:
        process = connection.process
        if process is None or process.stderr is None:
            return "(none)"
        try:
            data = await asyncio.wait_for(process.stderr.read(2000), timeout=0.5)
        except TimeoutError:
            return "(nothing on stderr)"
        return data.decode("utf-8", "replace").strip() or "(empty)"

    @staticmethod
    def _stderr_tail(connection: _Connection) -> str:
        return connection.stderr[-1] if connection.stderr else "(nothing captured)"

    # -- the seam ----------------------------------------------------------

    async def tools(self, constraints: ExecutionConstraints) -> tuple[McpTool, ...]:
        """Ask every configured server for its list, and keep only the allowed names."""
        found: list[McpTool] = []
        for name, config in self._servers.items():
            connection = await self._connection(name)
            result = await self._request(connection, "tools/list", {})
            listed = result.get("tools") or []
            if not isinstance(listed, list):
                raise McpUnavailable(
                    f"MCP server {name!r} answered tools/list with "
                    f"{type(listed).__name__}, not a list"
                )
            for raw in listed:
                if not isinstance(raw, Mapping):
                    continue
                tool_name = str(raw.get("name") or "")
                if not tool_name or tool_name not in config.tools:
                    continue
                found.append(
                    McpTool(
                        server=name,
                        name=tool_name,
                        description=str(raw.get("description") or ""),
                        input_schema=raw.get("inputSchema") or {},
                    )
                )
        return tuple(found)

    async def call(
        self,
        tool: McpTool,
        arguments: Mapping[str, Any],
        constraints: ExecutionConstraints,
    ) -> McpCallResult:
        config = self._servers.get(tool.server)
        if config is None:
            raise McpRefused(
                f"no MCP server named {tool.server!r} is configured. Configured: "
                f"{sorted(self._servers) or 'none'}"
            )
        if tool.name not in config.tools:
            raise McpRefused(
                f"tool {tool.name!r} is not in the allowlist for MCP server "
                f"{tool.server!r}. Allowed: {sorted(config.tools) or 'none'}"
            )
        connection = await self._connection(tool.server)
        result = await self._request(
            connection,
            "tools/call",
            {"name": tool.name, "arguments": dict(arguments)},
        )
        text = self._render(result.get("content"))
        if len(text) > self._max_chars:
            text = text[: self._max_chars] + "\n[truncated; the server returned more]"
        return McpCallResult(content=text, is_error=bool(result.get("isError")))

    @staticmethod
    def _render(content: Any) -> str:
        if not isinstance(content, list):
            return str(content)
        parts: list[str] = []
        for block in content:
            if not isinstance(block, Mapping):
                parts.append(str(block))
                continue
            kind = str(block.get("type") or "unknown")
            if kind == "text":
                parts.append(str(block.get("text") or ""))
            else:
                raw = block.get("data") or block.get("resource") or ""
                parts.append(_NON_TEXT.format(kind=kind, size=len(str(raw))))
        return "\n".join(part for part in parts if part != "")


class StdioMcpPlugin(PluginService):
    """Serves ``mcp``. Ships with no servers: the bundle's config lists none."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        cfg = dict(config or {})
        # Configuration before `super().__init__`, because that call registers the provide
        # row: raising afterwards would leave a half-built service visible to the kernel.
        servers = parse_servers(cfg.get("servers"))
        self.client = StdioMcpClient(
            servers,
            max_bytes=int(cfg.get("max_bytes", _DEFAULT_MAX_BYTES)),
            max_chars=int(cfg.get("max_chars", 60_000)),
        )
        super().__init__(ctx)

    async def tools(self, constraints: ExecutionConstraints) -> tuple[McpTool, ...]:
        return await self.client.tools(constraints)

    async def call(
        self,
        tool: McpTool,
        arguments: Mapping[str, Any],
        constraints: ExecutionConstraints,
    ) -> McpCallResult:
        return await self.client.call(tool, arguments, constraints)

    async def aclose(self) -> None:
        await self.client.aclose()
