"""The tools the skeleton ships: read a file, change a file, run a command.

Each one is a **consumer**: it depends on a seam, reaches the implementation
through the service container, and never imports a provider. That is what lets
the retriever, the sandbox or the workspace be swapped without touching a tool.

Two habits are enforced here rather than assumed:

- **Every tool checks its granted :class:`Capabilities`** before acting. The
  declared effects are what the tool *asked* for; the capabilities are what the
  pipeline actually *granted*, after constraints and the sandbox had their say.
  Trusting the declaration would make the sandbox advisory.
- **Every path is resolved against the workspace root and refused if it escapes.**
  The path guard already refuses escapes, so this is belt and braces — but a guard
  is a policy decision and a tool is the thing that touches the filesystem, and
  the second should not be relying on the first to be correct.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from forgepilot.kernel.errors import CapabilityNotGranted
from forgepilot.seams.sandbox import SandboxExecutionPolicy, SandboxMode
from forgepilot.seams.security import Operation, normalise_relative
from forgepilot.seams.tools import (
    ResourceTargets,
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolOutcome,
)

__all__ = ["BuiltinToolsPlugin", "build_tool_definitions"]

MAX_READ_BYTES = 512 * 1024


def _resolve(root: Path | None, raw: str) -> Path:
    """Resolve a workspace-relative path, refusing anything that escapes."""
    if root is None:
        raise CapabilityNotGranted(
            "this call has no workspace root, so there is nothing it may read or write"
        )
    relative = normalise_relative(raw)
    if relative is None:
        raise CapabilityNotGranted(
            f"path {raw!r} is absolute or escapes the workspace root; refusing"
        )
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise CapabilityNotGranted(f"path {raw!r} resolves outside the workspace root")
    return resolved


def _targets_paths(*keys: str):
    def describe(arguments: dict[str, Any]) -> ResourceTargets:
        return ResourceTargets(
            paths=tuple(str(arguments[k]) for k in keys if arguments.get(k) is not None)
        )

    return describe


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
async def _read_file(call: ToolCall, ctx: ToolContext) -> ToolOutcome:
    if not ctx.capabilities.fs_read:
        raise CapabilityNotGranted("this call was not granted filesystem read")

    import json

    args = json.loads(call.arguments_json or "{}")
    path = _resolve(ctx.workspace_root, str(args["path"]))
    if not path.exists():
        return ToolOutcome(content=f"no such file: {args['path']}", is_error=True)
    if path.is_dir():
        # Said plainly rather than letting an IsADirectoryError surface: "that is
        # a directory" is actionable, "IsADirectoryError: [Errno 21]" is a puzzle.
        try:
            entries = sorted(p.name for p in path.iterdir())[:50]
        except OSError:
            entries = []
        listing = ", ".join(entries) if entries else "(unreadable or empty)"
        return ToolOutcome(
            content=(
                f"{args['path']} is a directory, not a file. It contains: {listing}"
            ),
            is_error=True,
            meta={"is_directory": True, "entries": entries},
        )

    data = path.read_bytes()
    truncated = len(data) > MAX_READ_BYTES
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")

    start = int(args.get("start_line") or 1)
    end = int(args.get("end_line") or 0)
    lines = text.splitlines()
    if start > 1 or end:
        lines = lines[start - 1 : (end or len(lines))]

    return ToolOutcome(
        content="\n".join(lines),
        meta={"path": str(args["path"]), "truncated": truncated, "lines": len(lines)},
    )


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------
async def _write_file(call: ToolCall, ctx: ToolContext) -> ToolOutcome:
    if not ctx.capabilities.fs_write:
        raise CapabilityNotGranted("this call was not granted filesystem write")

    import json

    args = json.loads(call.arguments_json or "{}")
    path = _resolve(ctx.workspace_root, str(args["path"]))
    content = str(args.get("content", ""))

    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(content, encoding="utf-8")

    return ToolOutcome(
        content=f"{'updated' if existed else 'created'} {args['path']} ({len(content)} bytes)",
        meta={"path": str(args["path"]), "created": not existed, "bytes": len(content)},
    )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def build_tool_definitions(sandbox: Any) -> tuple[ToolDefinition, ...]:
    """The shipped tools, with the sandbox service closed over.

    ``run`` is bound to the sandbox rather than looking it up, so the tool cannot
    accidentally execute anything even if a future refactor hands it a context it
    should not have.
    """

    async def run_with_sandbox(call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        if not ctx.capabilities.exec:
            raise CapabilityNotGranted(
                "this call was not granted execution — either the tool did not need it, "
                "the constraints forbade it, or the host cannot contain a subprocess"
            )
        import json

        args = json.loads(call.arguments_json or "{}")
        argv = shlex.split(str(args.get("command", "")), posix=True)
        if not argv:
            return ToolOutcome(content="empty command", is_error=True)

        result = await sandbox.run(
            argv,
            policy=SandboxExecutionPolicy(
                mode=SandboxMode.WORKSPACE_WRITE,
                workspace_root=ctx.workspace_root or Path("."),
                session_id=ctx.session_id,
                network=ctx.constraints.network,
            ),
            cwd=ctx.workspace_root or Path("."),
            env={},
            timeout_s=ctx.timeout_s,
        )
        if not result.executed:
            # Never dress this up as a result. A model handed "(exit 0)" for a
            # command that never ran will summarise its output confidently.
            return ToolOutcome(
                content=(
                    f"the command was NOT executed: the configured sandbox "
                    f"({result.enforcement.value!r} enforcement) records calls without "
                    f"running them. There is no output to report, and anything inferred "
                    f"from this call would be invented. A sandbox provider that actually "
                    f"executes is required."
                ),
                is_error=True,
                meta={
                    "executed": False,
                    "enforcement": str(result.enforcement),
                    "argv": list(result.argv),
                },
            )

        body = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
        return ToolOutcome(
            content=body.strip() or f"(exit {result.exit_code})",
            is_error=result.exit_code != 0,
            meta={
                "exit_code": result.exit_code,
                "enforcement": str(result.enforcement),
                "timed_out": result.timed_out,
            },
        )

    return (
        ToolDefinition(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "workspace-relative path"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            effects=frozenset({ToolEffect.READ}),
            required_operation=Operation.READ_CODE,
            execute=_read_file,
            describe_targets=_targets_paths("path"),
        ),
        ToolDefinition(
            name="write_file",
            description="Create or overwrite a UTF-8 text file in the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "workspace-relative path"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            effects=frozenset({ToolEffect.READ, ToolEffect.WRITE}),
            required_operation=Operation.WRITE_CODE,
            execute=_write_file,
            describe_targets=_targets_paths("path"),
        ),
        ToolDefinition(
            name="run",
            description="Run a shell command in the workspace, inside the sandbox.",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            effects=frozenset({ToolEffect.EXEC}),
            required_operation=Operation.RUN_TESTS,
            execute=run_with_sandbox,
            describe_targets=lambda args: ResourceTargets(
                commands=(str(args.get("command", "")),)
            ),
        ),
    )


class BuiltinToolsPlugin:
    """Registers the shipped tools once both ``tools`` and ``sandbox`` are ready.

    Not a ``Service``: it provides nothing. It is a consumer that happens to run
    at boot, and declaring ``inject`` means it stays ``PENDING`` — and therefore
    registers nothing — on a host with no sandbox, which is the correct
    fail-closed behaviour.
    """

    inject = ["tools", "sandbox"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        self.ctx = ctx
        self.config = dict(config or {})

    def init(self) -> Any:
        registry = self.ctx.tools
        sandbox = self.ctx.sandbox
        unregisters = [
            registry.register(definition)
            for definition in build_tool_definitions(sandbox)
        ]

        def cleanup() -> None:
            for unregister in unregisters:
                unregister()

        return cleanup
