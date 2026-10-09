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
from dataclasses import replace
from pathlib import Path
from typing import Any

from forgepilot.kernel.errors import CapabilityNotGranted
from forgepilot.runtime.sandbox_env import command_env
from forgepilot.seams.sandbox import SandboxExecutionPolicy, SandboxMode
from forgepilot.seams.retriever import WORKING_TREE, SearchQuery
from forgepilot.seams.security import Operation, normalise_relative
from forgepilot.seams.tools import (
    ResourceTargets,
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolOutcome,
)

__all__ = [
    "BuiltinToolsPlugin",
    "CodeSearchToolPlugin",
    "build_search_definitions",
    "build_tool_definitions",
]

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


# ---------------------------------------------------------------------------
# code_search
# ---------------------------------------------------------------------------

def build_search_definitions(retriever: Any) -> tuple[ToolDefinition, ...]:
    """The search tool, bound to a retriever.

    Its own function — and its own plugin — so that a host with no retriever
    loses *search* rather than losing every tool.
    """
    definitions = [
            ToolDefinition(
                name="code_search",
                description=(
                    "Search the code for a regular expression (ripgrep syntax). Use it to find "
                    "where something is defined or used before reading files. Returns file "
                    "paths by default; ask for `content` to see matching lines."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "regular expression, ripgrep syntax",
                        },
                        "output_mode": {
                            "type": "string",
                            "enum": list(OUTPUT_MODES),
                            "description": (
                                "files_with_matches (default) lists paths only; content shows "
                                "matching lines; count totals per file"
                            ),
                        },
                        "glob": {
                            "type": "string",
                            "description": "restrict to files matching this glob, e.g. 'src/**'",
                        },
                        "repo": {
                            "type": "string",
                            "description": "restrict to one repository, as 'host/namespace/name'",
                        },
                        "max_results": {"type": "integer", "description": "cap; default 100"},
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
                effects=frozenset({ToolEffect.READ}),
                required_operation=Operation.READ_CODE,
                execute=lambda call, ctx: _code_search(call, ctx, retriever),
                # No filesystem targets: a search addresses a repository, not a path,
                # and reporting the glob as a path would let a path guard reason about
                # something that is not one.
                describe_targets=lambda arguments: ResourceTargets(),
            ),
    ]
    # The retriever is closed over, like the sandbox above.
    return tuple(
        replace(definition, execute=lambda call, ctx: _code_search(call, ctx, retriever))
        for definition in definitions
    )

#: How results are rendered. The default is the *cheapest* one on purpose: a
#: model that wanted to know which files matter, and instead receives forty
#: matching lines, has spent context on something it did not ask for.
OUTPUT_MODES = ("files_with_matches", "content", "count")

#: Results returned when the caller does not say. High enough to be useful, low
#: enough that a broad pattern does not arrive as a wall of text.
DEFAULT_MAX_RESULTS = 100


def _targets_paths(*keys: str):
    def describe(arguments: dict[str, Any]) -> ResourceTargets:
        return ResourceTargets(
            paths=tuple(str(arguments[k]) for k in keys if arguments.get(k) is not None)
        )

    return describe


# ---------------------------------------------------------------------------
# code_search
# ---------------------------------------------------------------------------
async def _code_search(call: ToolCall, ctx: ToolContext, retriever: Any) -> ToolOutcome:
    """Search code through the `Retriever` seam.

    The seam is what makes this swappable: this tool names a capability, not a
    program. Under the target deployment the same call reaches an enterprise
    index; today it reaches ripgrep. The tool never learns which, and must not.
    """
    import json

    if not ctx.capabilities.fs_read:
        raise CapabilityNotGranted("this call was not granted filesystem read")

    try:
        arguments = json.loads(call.arguments_json or "{}")
    except json.JSONDecodeError as exc:
        return ToolOutcome(
            content=f"arguments are not valid JSON: {exc}", is_error=True, meta={"phase": "args"}
        )

    pattern = str(arguments.get("pattern") or "").strip()
    if not pattern:
        return ToolOutcome(
            content="`pattern` is required: a search with no pattern is a listing, "
            "and `read_file` on a directory does that better.",
            is_error=True,
            meta={"phase": "args"},
        )

    mode = str(arguments.get("output_mode") or "files_with_matches")
    if mode not in OUTPUT_MODES:
        return ToolOutcome(
            content=f"unknown output_mode {mode!r}; expected one of "
            f"{', '.join(OUTPUT_MODES)}.",
            is_error=True,
            meta={"phase": "args"},
        )

    # Which repositories. The caller has to *narrow* — the seam refuses to read
    # an empty tuple as "all of them" — but it cannot narrow to repos it has no
    # way to name, and the system prompt shows it a prefix, not a list. So the
    # candidates come from the provider and the *grant* comes from the caller.
    # **The grant is checked before coverage, and the order is the point.** Asking
    # about a repository the caller may not read and being told "no checkout is
    # configured" confirms that it is *not* there — a probe for which repositories
    # this deployment has. The same reasoning that makes the HTTP layer answer 404
    # rather than 403 for someone else's session.
    #
    # Matched by slug rather than parsed: the Retriever seam has no `resolve`
    # (that is a Vcs method), and adding one so this tool can turn a string into a
    # RepoRef would be inventing a contract to save three lines.
    permitted = tuple(
        repo for repo in retriever.repositories() if ctx.constraints.allows_repo(repo)
    )

    if "repo" in arguments and arguments["repo"]:
        wanted = str(arguments["repo"])
        permitted = tuple(repo for repo in permitted if repo.slug == wanted)
        if not permitted:
            return ToolOutcome(
                content=f"{wanted!r} is not a repository this principal may search.",
                is_error=True,
                meta={"phase": "args"},
            )

    if not permitted:
        return ToolOutcome(
            content="no repository this principal may read is available to search.",
            is_error=True,
            meta={"phase": "args"},
        )

    try:
        cap = int(arguments.get("max_results") or DEFAULT_MAX_RESULTS)
    except (TypeError, ValueError):
        return ToolOutcome(
            content="`max_results` must be an integer", is_error=True, meta={"phase": "args"}
        )
    cap = max(1, cap)

    glob = arguments.get("glob")
    query = SearchQuery(
        pattern=pattern,
        repos=permitted,
        # The working tree, not "HEAD": an agent editing a checkout has no
        # revision to name, and "HEAD" would claim a commit while the results
        # include uncommitted edits. Each hit reports the commit actually read.
        ref=WORKING_TREE,
        globs=(str(glob),) if glob else (),
        # One extra, so a capped result can be *reported* as capped rather than
        # looking like the whole answer.
        max_hits=cap + 1,
    )

    hits = tuple(await retriever.search(query, ctx.constraints))
    truncated = len(hits) > cap
    hits = hits[:cap]

    if not hits:
        return ToolOutcome(
            content=f"no match for {pattern!r} in {', '.join(r.slug for r in permitted)}.",
            meta={"hits": 0, "truncated": False, "outputMode": mode},
        )

    rendered = _render_hits(hits, mode)
    if truncated:
        rendered += (
            f"\n\n[truncated: showing the first {cap} of more. Narrow the pattern or the "
            f"glob to see the rest.]"
        )

    return ToolOutcome(
        content=rendered,
        meta={
            "hits": len(hits),
            "truncated": truncated,
            "outputMode": mode,
            "repos": [r.slug for r in permitted],
        },
    )


def _render_hits(hits: tuple[Any, ...], mode: str) -> str:
    if mode == "content":
        return "\n".join(f"{h.location.path}:{h.line}: {h.text}" for h in hits)

    if mode == "count":
        counts: dict[str, int] = {}
        for hit in hits:
            counts[hit.location.path] = counts.get(hit.location.path, 0) + 1
        lines = [f"{path}: {n}" for path, n in sorted(counts.items())]
        return "\n".join(lines) + f"\n\n{len(hits)} matches in {len(counts)} files"

    # files_with_matches — the default: names, no match text.
    seen: dict[str, None] = {}
    for hit in hits:
        seen.setdefault(hit.location.path, None)
    paths = list(seen)
    listing = "\n".join(paths)
    return f"found {len(paths)} file(s) matching:\n{listing}"


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
    should not have. ``ToolContext`` carries no service container for the same
    reason.

    ``code_search`` is deliberately *not* here; it lives in
    :func:`build_search_definitions`, behind its own plugin. Putting both in one
    plugin would mean injecting a retriever into the thing that provides
    ``write_file`` — and then a profile with no retriever would lose every tool,
    including the ones that have nothing to do with search.
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
            env=command_env(),
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


class CodeSearchToolPlugin:
    """Registers ``code_search`` once ``tools`` and ``retriever`` are ready.

    A separate plugin from :class:`BuiltinToolsPlugin` because it needs a
    different service. Folding both into one would mean a host with no retriever
    loses ``read_file`` — the capability it does have — because of the one it does
    not. Fail-closed should mean "this tool is absent", not "the tool set is
    absent".
    """

    inject = ["tools", "retriever"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        self.ctx = ctx
        self.config = dict(config or {})

    def init(self) -> Any:
        registry = self.ctx.tools
        retriever = self.ctx.retriever
        unregisters = [
            registry.register(definition)
            for definition in build_search_definitions(retriever)
        ]

        def cleanup() -> None:
            for unregister in unregisters:
                unregister()

        return cleanup
