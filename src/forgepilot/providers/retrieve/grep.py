"""Local text search over a checkout — service ``retriever``.

This is the default, and it is deliberately the simplest thing that works:
``ripgrep`` over directories the provider has been told about. It replaces a
mock that returned canned hits, which meant nothing in the system had ever
searched a file.

**Ripgrep or nothing.** There is no Python fallback, and that is a decision
rather than laziness: a fallback would be a different search — different regex
dialect, different glob semantics, different handling of binary files and
``.gitignore`` — and callers would have no way to tell which one produced a hit.
The project already refuses to present a text match and an AST parse as the same
evidence; two text searches are no different. When the binary is missing the
provider says so, with the command that fixes it.

**What this is not.** The target environment sees repositories as GVFS virtual
filesystems, where walking the local tree triggers mass on-demand downloads.
Under that deployment this provider is an *overlay* — the branch diff and the
working tree, a small fully-materialised set — and global search is the
enterprise index behind the same Protocol. The seam is what makes that a
configuration change rather than a rewrite; this provider is the thing that makes
the seam testable today.

**The ref a hit carries is resolved, not echoed.** Reporting back the ref the
caller asked for would claim the results came from that revision, which for a
working-tree search is exactly the false confidence the seam warns about. So the
commit is read from the checkout, and uncommitted edits are marked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef
from forgepilot.seams.retriever import (
    SERVICE_NAME,
    CodeHit,
    FileSlice,
    RetrieverCapabilities,
    SearchQuery,
    SymbolQuery,
)
from forgepilot.seams.security import ExecutionConstraints

__all__ = ["GrepRetrieverPlugin", "RipgrepMissing", "UnknownCheckout", "WORKTREE"]

#: The ref reported for a checkout that is not a git repository. Not the queried
#: ref: there is no revision to name, and inventing one would be a claim.
WORKTREE = "WORKTREE"


class RipgrepMissing(RuntimeError):
    """``rg`` is not on PATH. Raised with the command that installs it."""


class UnknownCheckout(RuntimeError):
    """No local directory is configured for a repository the caller may read."""


class GrepRetrieverPlugin(PluginService):
    """Serves ``retriever`` using ripgrep over configured checkouts.

    Config::

        roots:  {"git.corp/team/svc-a": "/srv/checkouts/svc-a"}
        binary: "rg"           # optional
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._roots: dict[str, Path] = {
            str(slug): Path(path).resolve() for slug, path in (cfg.get("roots") or {}).items()
        }
        self._binary = str(cfg.get("binary") or "")
        #: Searches refused for lack of authority, so a test can assert a refusal
        #: happened rather than infer it from an absence.
        self.denied_calls = 0

    # -- capabilities ------------------------------------------------------
    def capabilities(self) -> RetrieverCapabilities:
        return RetrieverCapabilities(
            supports_symbols=False,
            supports_remote=False,
            # Grepping a checkout sees uncommitted edits by construction, and a
            # caller comparing two providers needs to know that.
            covers_working_tree=True,
        )

    # -- the binary --------------------------------------------------------
    def binary(self) -> str:
        """Locate ripgrep, or explain what to do about it.

        Resolved lazily rather than at construction: a profile with no ripgrep
        can still boot for the modes that do not search, and the failure lands
        where it is actionable.

        Three places, in order — an explicit ``binary`` in config, ``PATH``, and
        **next to the running interpreter**. The third is not a flourish: a
        deployment that installs ripgrep as a Python package gets it in the
        virtualenv's ``Scripts``/``bin`` directory, which is on ``PATH`` only
        when the venv is *activated*. Running the interpreter directly — which
        every command in this repository does, and which a service unit does — is
        exactly when ``PATH`` does not have it. Claude Code goes further and
        embeds ripgrep in its own binary; finding it beside the interpreter is
        the same idea with less machinery.
        """
        if self._binary:
            return self._binary

        found = shutil.which("rg")
        if found:
            return found

        beside = Path(sys.executable).parent / ("rg.exe" if os.name == "nt" else "rg")
        if beside.exists():
            return str(beside)

        raise RipgrepMissing(
            f"ripgrep was not found: not configured, not on PATH, and not beside the "
            f"interpreter at {beside}. Install it (`pip install ripgrep-bin`, or your "
            f"platform's package), or set `binary:` to its path. There is deliberately "
            f"no fallback scanner — a second implementation would be a different search "
            f"with different glob semantics, and a caller could not tell which one "
            f"produced a hit."
        )

    # -- helpers -----------------------------------------------------------
    def _root(
        self, repo: RepoRef, constraints: ExecutionConstraints, override: str | None = None
    ) -> Path:
        """Where to search. The caller's working tree wins over the configured path.

        **The two are different questions and answering one with the other is
        how a search silently returns the wrong code.** The configured root says
        where a repository *lives*; a session works in a checkout cut from it,
        and after the first edit those are different content. That is not
        hypothetical: an agent editing a worktree searched the repository it was
        cut from and got pre-edit results, which looked like an answer.

        The grant is still checked against ``repo`` first, and still here rather
        than left to the caller. An override changes which copy is read, never
        which repositories may be read.
        """
        if not constraints.allows_repo(repo):
            self.denied_calls += 1
            raise PermissionError(f"repo {repo.slug!r} is not permitted")

        if override:
            path = Path(override).resolve()
            if not path.is_dir():
                self.denied_calls += 1
                raise UnknownCheckout(
                    f"the caller's working tree for {repo.slug!r} is {path}, which is "
                    f"not a directory. Refusing rather than falling back to the "
                    f"configured checkout — a quiet fallback would answer a question "
                    f"about the working tree with content from somewhere else."
                )
            return path

        root = self._roots.get(repo.slug)
        if root is None:
            self.denied_calls += 1
            raise UnknownCheckout(
                f"no local checkout is configured for {repo.slug!r}. A repository this "
                f"provider cannot reach is not an empty repository — searching nothing "
                f"and returning nothing would be indistinguishable from 'no matches'. "
                f"Configured: {sorted(self._roots)}"
            )
        return root

    def repositories(self) -> tuple[RepoRef, ...]:
        """The checkouts this provider was configured with.

        Covers, not a grant: the caller intersects this with its own constraints.
        """
        out: list[RepoRef] = []
        for slug in sorted(self._roots):
            parts = slug.split("/")
            if len(parts) == 3:
                out.append(RepoRef(host=parts[0], namespace=parts[1], name=parts[2]))
        return tuple(out)

    def _ref(self, root: Path, requested: str) -> str:
        """The revision the results actually came from.

        Read from the checkout rather than echoed from the query. Uncommitted
        edits are marked, because a hit from a dirty tree is not a hit from any
        commit — and a reviewer comparing two runs would otherwise see the same
        ref for different content.
        """
        head = self._git(root, "rev-parse", "HEAD")
        if head is None:
            return WORKTREE  # a directory that is not a repository has no revision
        dirty = self._git(root, "status", "--porcelain")
        return f"{head}+dirty" if dirty else head

    @staticmethod
    def _git(root: Path, *args: str) -> str | None:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None

    # -- Retriever ---------------------------------------------------------
    async def search(
        self, query: SearchQuery, constraints: ExecutionConstraints
    ) -> Sequence[CodeHit]:
        hits: list[CodeHit] = []
        for repo in query.repos:
            root = self._root(repo, constraints, query.root)
            ref = self._ref(root, query.ref)
            for record in self._run(root, query):
                hits.append(self._hit(record, repo, root, ref))
                if len(hits) >= query.max_hits:
                    return tuple(hits)
        return tuple(hits)

    def _run(self, root: Path, query: SearchQuery) -> list[dict]:
        argv = [self.binary(), "--json", "--max-count", str(query.max_hits)]
        if query.literal:
            argv.append("--fixed-strings")
        for glob in query.globs:
            argv.extend(["--glob", glob])
        # `--` before the pattern so a pattern beginning with `-` is not read as
        # a flag, and `.` last so the search root is explicit.
        argv.extend(["--", query.pattern, "."])

        try:
            completed = subprocess.run(
                argv,
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError as exc:
            # The path was resolved a moment ago; something removed it since, or
            # the config named a path that is not executable. Say which.
            raise RipgrepMissing(f"could not run {argv[0]!r}: {exc}") from exc
        # 0 = matches, 1 = none, 2 = a real failure (bad regex, unreadable path).
        if completed.returncode == 1:
            return []
        if completed.returncode != 0:
            raise RuntimeError(
                f"search failed: {completed.stderr.strip() or completed.stdout.strip()}"
            )

        out: list[dict] = []
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("type") == "match":
                out.append(record)
        return out

    def _hit(self, record: dict, repo: RepoRef, root: Path, ref: str) -> CodeHit:
        data = record["data"]
        # Ripgrep reports paths relative to the search root, with the platform's
        # separator and a leading `./`. Normalised to the `/`-separated,
        # repo-relative form everything else in this system uses — a hit whose
        # path is `.\src\login.py` would not match a diff, a group, or a changed
        # path, and would look like a file that does not exist.
        #
        # Only the *platform's* separator is rewritten: on Linux `\` is a legal
        # filename character, and replacing it there would silently rename files.
        path = data["path"]["text"]
        if os.sep != "/":
            path = path.replace(os.sep, "/")
        while path.startswith("./"):
            path = path[2:]
        line = int(data["line_number"])
        text = data["lines"]["text"].rstrip("\n")

        submatch = (data.get("submatches") or [{}])[0]
        column = self._column(text, int(submatch.get("start", 0)))

        return CodeHit(
            # `root` travels with the hit so `open` can read the copy this search
            # actually saw: the caller's working tree when `query.root` named one,
            # and the configured checkout otherwise. Without it, `open` reads the
            # configured root and answers a question about the working tree with
            # content from somewhere else.
            location=CodeRef(repo=repo, path=path, ref=ref, root=str(root)),
            line=line,
            column=column,
            text=text,
        )

    @staticmethod
    def _column(text: str, byte_offset: int) -> int:
        """Convert ripgrep's byte offset into a 1-based character column.

        Bytes rather than characters: a line with any non-ASCII before the match
        would otherwise be reported several columns too far right.
        """
        prefix = text.encode("utf-8")[:byte_offset]
        return len(prefix.decode("utf-8", errors="replace")) + 1

    async def symbols(
        self, query: SymbolQuery, constraints: ExecutionConstraints
    ) -> Sequence[Any]:
        """Not supported, and ``capabilities()`` says so.

        A regex pretending to find definitions would produce hits that look
        structural and are not — the caller has no way to tell which it got.
        """
        return ()

    async def open(
        self, location: CodeRef, span: LineSpan, constraints: ExecutionConstraints
    ) -> FileSlice:
        # `location.root` is the working tree the hit came from, when it came from
        # one. `_root` resolves and validates it (a working tree that is not there
        # raises `UnknownCheckout` rather than falling back to the configured
        # checkout), so reading "the same copy the search saw" is structural here
        # instead of a convention the caller has to remember.
        root = self._root(location.repo, constraints, location.root)

        # The caller is given the ref that was *actually* read, so "I asked for
        # my branch and got trunk" is detectable rather than silent.
        ref = self._ref(root, location.ref)
        target = (root / location.path).resolve()
        if root not in target.parents and target != root:
            raise PermissionError(
                f"{location.path!r} resolves outside {root}; refusing to read it"
            )

        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            # Named the way the caller addressed it, not as an OS error: a model that
            # asked for `repo:path` gets an answer about `repo:path`, and the message is
            # the same on every platform (a raw `[Errno 2]` carries a Windows path with
            # backslashes, which matches nothing the model was shown).
            raise FileNotFoundError(
                f"no {location.repo.slug}:{location.path} in the checkout at {root}"
            ) from None
        start = max(1, span.start)
        end = min(len(lines), span.end) if span.end else len(lines)
        return FileSlice(
            location=CodeRef(
                repo=location.repo,
                path=location.path,
                ref=ref,
                root=str(root),
            ),
            span=LineSpan(start=start, end=end),
            text="\n".join(lines[start - 1 : end]),
        )
