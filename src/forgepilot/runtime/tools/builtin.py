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

from dataclasses import replace
from pathlib import Path
from typing import Any

from forgepilot.kernel.errors import CapabilityNotGranted
from forgepilot.runtime.commandline import shell_argv
from forgepilot.runtime.credentials import resolve_placeholders, scrub_values
from forgepilot.runtime.sandbox_denial import likely_denial, not_executed_reason
from forgepilot.runtime.sandbox_env import command_env
from forgepilot.seams.retriever import WORKING_TREE, SearchQuery
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
        # **The tree this session is working in**, which is not the same as the
        # path the repository was configured at. An agent edits a checkout; the
        # configured path is where the repository lives. Searching the latter
        # returns pre-edit content and looks exactly like an answer — and on a
        # repo host whose mirror is bare, it returns nothing at all while saying
        # "no match", which reads as "that symbol does not exist".
        root=str(ctx.workspace_root) if ctx.workspace_root else None,
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
# edit_file
# ---------------------------------------------------------------------------
def _match_offsets(text: str, needle: str) -> list[int]:
    """Non-overlapping match offsets, in order."""
    offsets: list[int] = []
    index = text.find(needle)
    while index != -1:
        offsets.append(index)
        index = text.find(needle, index + len(needle))
    return offsets


def _flatten_map(text: str) -> tuple[str, list[int]]:
    """``text`` with CRLF flattened to LF, plus a map back to the original offsets.

    ⚠️ **A map rather than an index, because the substitution changes length.** A
    ``\\r\\n`` is two characters and its flattened form is one, so a match found at
    offset *i* in the flattened text is **not** at offset *i* in the file — and a
    slice taken that way edits the wrong characters while reporting success. Position
    *k* of the flattened text came from ``positions[k]`` of the original, and the
    final entry is the end offset so a slice end has a mapping too.
    """
    out: list[str] = []
    positions: list[int] = []
    index = 0
    while index < len(text):
        if text.startswith("\r\n", index):
            out.append("\n")
            positions.append(index)
            index += 2
            continue
        out.append(text[index])
        positions.append(index)
        index += 1
    positions.append(len(text))
    return "".join(out), positions


def _uniqueness_refusal(path: str, count: int) -> ToolOutcome:
    """Refuse to guess which occurrence was meant.

    This is the whole reason `edit_file` exists rather than a `str.replace` helper:
    a substring that appears more than once is the normal case in real code, and the
    alternatives are both bad — replace the first (silently edits the wrong site) or
    replace all (silently edits sites nobody mentioned). Naming the count and the two
    ways forward costs one turn; guessing costs a review.
    """
    return ToolOutcome(
        content=(
            f"old_string appears {count} times in {path}. This tool will not choose "
            f"one for you: include enough surrounding lines to make it unique, or "
            f"pass replace_all to change every occurrence deliberately."
        ),
        is_error=True,
        meta={"path": path, "occurrences": count, "ambiguous": True},
    )


async def _edit_file(call: ToolCall, ctx: ToolContext) -> ToolOutcome:
    """Replace an exact piece of a file, refusing anything it cannot do exactly.

    Three rules, and each exists because the alternative fails *quietly*:

    - **The search text must be found.** A near miss — one space of indentation, a
      line ending the model normalised — is reported with the file's size, not
      guessed at. Writing the file unchanged and reporting success would be the
      worst outcome available.
    - **It must be found once**, unless ``replace_all`` says otherwise. See
      :func:`_uniqueness_refusal`.
    - **The file is read the way `read_file` reads it** (raw bytes, no newline
      translation), so the text the model was shown is the text this searches. The
      two tools disagreeing about what a file *is* would make every copy-pasted
      edit miss — and the miss would look like the model's mistake.
    """
    if not ctx.capabilities.fs_write:
        raise CapabilityNotGranted("this call was not granted filesystem write")

    import json

    args = json.loads(call.arguments_json or "{}")
    path = _resolve(ctx.workspace_root, str(args["path"]))
    old = str(args.get("old_string", ""))
    new = str(args.get("new_string", ""))
    replace_all = bool(args.get("replace_all", False))
    label = str(args["path"])

    if not old:
        return ToolOutcome(
            content=(
                "old_string is empty, and an empty search matches everywhere — it "
                "would insert your text at every position in the file. Use write_file "
                "to replace a file wholesale."
            ),
            is_error=True,
            meta={"phase": "arguments"},
        )
    if old == new:
        return ToolOutcome(
            content="old_string and new_string are identical, so this edit changes nothing",
            is_error=True,
            meta={"phase": "arguments"},
        )

    if not path.exists():
        return ToolOutcome(content=f"no such file: {label}", is_error=True)
    if path.is_dir():
        return ToolOutcome(
            content=f"{label} is a directory, not a file", is_error=True,
            meta={"is_directory": True},
        )

    raw = path.read_bytes()
    if len(raw) > MAX_READ_BYTES:
        # ⚠️ Refused rather than truncated. `read_file` truncates because showing
        # part of a file is useful; writing back part of a file destroys the rest,
        # and "it edited a big file" and "it deleted the tail of a big file" would
        # produce the same success message.
        return ToolOutcome(
            content=(
                f"{label} is {len(raw)} bytes, larger than this tool edits "
                f"({MAX_READ_BYTES}). Refusing rather than rewriting a truncated "
                f"file — use a narrower edit or write_file."
            ),
            is_error=True,
            meta={"phase": "size", "bytes": len(raw)},
        )

    text = raw.decode("utf-8", errors="replace")
    offsets = _match_offsets(text, old)
    normalised = False

    if not offsets:
        # Second attempt: the model may have normalised the line endings when it
        # wrote the search text, which is the one difference a copy-paste makes
        # invisible. Matching is retried on a flattened copy; the *result* is still
        # spliced into the original bytes, so untouched text is byte-identical.
        flat, positions = _flatten_map(text)
        flat_offsets = _match_offsets(flat, old.replace("\r\n", "\n"))
        if not flat_offsets:
            return ToolOutcome(
                content=(
                    f"old_string was not found in {label} ({len(text.splitlines())} "
                    f"lines read). It must match the file exactly, including "
                    f"indentation and blank lines — read the file and copy the text "
                    f"you want to replace."
                ),
                is_error=True,
                meta={"path": label, "occurrences": 0},
            )
        if len(flat_offsets) > 1 and not replace_all:
            return _uniqueness_refusal(label, len(flat_offsets))
        offsets = [
            positions[index] for index in flat_offsets
        ]
        ends = [positions[index + len(old.replace("\r\n", "\n"))] for index in flat_offsets]
        normalised = True
    else:
        if len(offsets) > 1 and not replace_all:
            return _uniqueness_refusal(label, len(offsets))
        ends = [index + len(old) for index in offsets]

    if not replace_all:
        offsets, ends = offsets[:1], ends[:1]

    # ⚠️ **The inserted text takes the file's own line endings.** Without this an
    # edit into a CRLF file splices LF lines into it, and the mixed endings show up
    # as a whole-file diff in review — a one-line change presented as hundreds.
    replacement = new
    if "\r\n" in text and "\r\n" not in new:
        replacement = new.replace("\n", "\r\n")

    result = text
    # `strict=True`: `offsets` and `ends` are built in lockstep from the same match
    # list, so unequal lengths would be a bug in this function rather than a case to
    # absorb quietly.
    for start, end in reversed(list(zip(offsets, ends, strict=True))):
        result = result[:start] + replacement + result[end:]

    # `newline=""`: the default translates `\n` to `os.linesep` on the way out, so
    # on Windows a file already holding CRLF would come back with `\r\r\n`. The text
    # is spliced, not regenerated, and it must be written as it stands.
    path.write_text(result, encoding="utf-8", newline="")

    return ToolOutcome(
        content=(
            f"edited {label}: replaced {len(offsets)} occurrence(s) of the search "
            f"text, {len(result) - len(text):+d} bytes"
            + (
                " (the search text was matched with line endings normalised, and the "
                "replacement was written with the file's own)"
                if normalised
                else ""
            )
        ),
        meta={
            "path": label,
            "occurrences": len(offsets),
            "replaceAll": replace_all,
            "newlineNormalised": normalised,
            "bytes": len(result),
        },
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
        command = str(args.get("command", ""))
        if not command.strip():
            return ToolOutcome(content="empty command", is_error=True)

        # **Through a shell, unlike the project's own checks.** A model writes a
        # shell line; `pytest -q 2>&1 | tail -30` is one that was actually sent,
        # and executing it as an argv list hands `2>&1` and `tail` to pytest as
        # file arguments. This is expressiveness, not authority — the model could
        # have asked for any argv either way, and the boundary is the sandbox.
        #
        # The `posix=True` split that used to be here was a separate defect, and
        # the same one `run_checks` had already been fixed for: it ate the
        # backslashes out of `C:\…\python.exe`, nothing was spawned, and the
        # model was told the *sandbox* had refused. Hence `shell_argv` — the one
        # place that knows how this platform starts a shell.
        # ``${NAME}`` is resolved here — the last moment before a process sees the
        # command, and nowhere earlier. The arm phase has already refused the call
        # if a name could not be resolved or if the line carried a literal secret,
        # so what is left is the substitution and the obligation that comes with
        # it: put the placeholder back into anything that will be recorded.
        command, secrets = await resolve_placeholders(command, ctx.credentials)

        argv = shell_argv(command)

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
            # `executed=False` says exactly one thing: nothing ran. It does **not**
            # say the sandbox refused — an executable that does not exist is also
            # "not executed" — and the two need opposite responses. The message
            # this replaced asserted the sandbox was the cause either way, and a
            # model that read "the sandbox cannot execute" stopped trying to run
            # anything at all: Codex #41320 with the blame on the wrong party.
            # `not_executed_reason` is where the two are told apart.
            #
            # Never dress this up as a result, either: a model handed "(exit 0)"
            # for a command that never ran will summarise its output confidently.
            return ToolOutcome(
                content=scrub_values(
                    "the command was NOT executed, so there is no output, and anything "
                    "inferred from this call would be invented.\n"
                    # `not_executed_reason` prints the argv it was handed — which is
                    # the *substituted* one. Without this scrub the value lands in a
                    # tool result and then in the log, through the diagnostics rather
                    # than through the command.
                    + not_executed_reason(result),
                    secrets,
                ),
                is_error=True,
                meta={
                    "executed": False,
                    "exit_code": result.exit_code,
                    "enforcement": str(result.enforcement),
                    # Scrubbed, not raw. This `argv` is the *substituted* one, and
                    # it lands in a durable event — the one place a resolved value
                    # must never appear, and the reason `resolve_placeholders`
                    # returns what it substituted.
                    "argv": [scrub_values(part, secrets) for part in result.argv],
                    "secrets": sorted(secrets),
                },
            )

        body = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
        body = body.strip()

        # Say out loud when the *boundary* is a plausible cause. Codex #41320 is
        # the failure this prevents: with the sandbox's role dropped, the model
        # sees a bare exit code and goes looking for another execution
        # environment instead of taking the escalation path it was given.
        #
        # "possibly", because the classifier is a heuristic — and a confident
        # wrong answer here sends the model down the escalation path for what is
        # really a bug in the code under test.
        denial = likely_denial(result)
        if denial:
            body = (
                f"{body}\n\n[possibly the sandbox: {denial}. This is not proof — the "
                f"output above is the command's own. If the command needs access the "
                f"boundary does not grant, say so rather than trying a different route.]"
            ).strip()

        # The command's own output is the third place a value can surface: an
        # `echo`, a `set -x`, a stack trace that prints the argv. Scrubbed for the
        # same reason as the argv meta — and this is where a rule set would *not*
        # help, because the whole point of the channel is that a secret need not
        # look like one for a rule to catch it.
        body = scrub_values(body, secrets)

        return ToolOutcome(
            content=body or f"(exit {result.exit_code})",
            is_error=result.exit_code != 0,
            meta={
                "exit_code": result.exit_code,
                "enforcement": str(result.enforcement),
                "timed_out": result.timed_out,
                "sandboxSuspected": bool(denial),
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
            name="edit_file",
            description=(
                "Replace an exact piece of an existing file. `old_string` must match "
                "the file exactly — including indentation — and must appear exactly "
                "once unless `replace_all` is true. Prefer this over write_file: it "
                "changes one place instead of re-emitting the whole file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "workspace-relative path"},
                    "old_string": {
                        "type": "string",
                        "description": "the exact text to replace, copied from read_file",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "what to replace it with; empty deletes it",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": (
                            "replace every occurrence; default false, and without it an "
                            "ambiguous search is refused rather than guessed"
                        ),
                    },
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
            # READ as well as WRITE: it reads the file to match against. Declaring
            # only WRITE would claim a capability set this tool cannot work without —
            # and `arm` intersects the declaration, so the narrower claim would grant
            # an edit on a call that cannot read.
            effects=frozenset({ToolEffect.READ, ToolEffect.WRITE}),
            required_operation=Operation.WRITE_CODE,
            execute=_edit_file,
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
