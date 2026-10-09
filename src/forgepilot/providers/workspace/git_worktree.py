"""One real git worktree per session — service ``workspace``.

The temp-directory provider copies files and calls the result a workspace. That
is honest for a skeleton and useless for a deployment: nothing about the
directory is a repository, so `git status`, a branch, or a commit has nothing to
attach to. Everything the delivery loop is *for* — a commit, a push, a reviewable
diff — needs a repository to exist.

**A worktree, not a clone.** `git worktree add` gives each session its own
working directory over the repository's **single** object store. A clone would
copy the history per session, which under the target environment's 300GB figure
is the difference between a checkout and a disk-space incident. It also means two
sessions on one repository cannot see each other's edits, while neither pays to
duplicate the objects.

The worktree path is **derived, not configured**: it comes from the session id,
under a root the provider owns. A configurable per-session path would let two
sessions be pointed at one directory, and the symptom of that is one agent
committing another agent's half-finished work.

The root is resolved before it is returned. It becomes the sandbox policy's
``workspace_root``, so an unresolved path containing a symlink or ``..`` would
silently widen the boundary the sandbox enforces.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.refs import RepoRef, SessionId
from forgepilot.seams.workspace import (
    SERVICE_NAME,
    UnifiedDiff,
    UnknownRepository,
    WorkspaceHandle,
)

__all__ = ["GitWorktreeWorkspacePlugin", "GitFailed"]


class GitFailed(RuntimeError):
    """A git invocation failed. Carries git's own message, not a paraphrase."""

    def __init__(self, argv: Sequence[str], stderr: str) -> None:
        super().__init__(f"git {' '.join(argv)} failed: {stderr.strip()}")
        self.argv = tuple(argv)
        self.stderr = stderr


class GitWorktreeWorkspacePlugin(PluginService):
    """Serves ``workspace`` with a git worktree per session.

    Config::

        repos:          {"git.corp/team/svc-a": "/srv/repos/svc-a"}   # slug -> local source
        worktree_root:  "${FORGEPILOT_HOME}/worktrees"
        branch_prefix:  "forgepilot"                                  # optional
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})

        self._repos: dict[str, Path] = {
            str(slug): Path(path).resolve() for slug, path in (cfg.get("repos") or {}).items()
        }
        root = cfg.get("worktree_root")
        if not root:
            raise ValueError(
                "worktree_root is required: a session's checkout must live somewhere the "
                "provider owns, and defaulting to a temp directory would lose the work on "
                "reboot."
            )
        self._worktree_root = Path(root).resolve()
        self._worktree_root.mkdir(parents=True, exist_ok=True)
        self._branch_prefix = str(cfg.get("branch_prefix", "forgepilot"))

        self._handles: dict[SessionId, WorkspaceHandle] = {}

    # -- git ---------------------------------------------------------------
    def _git(self, *args: str, cwd: Path | None = None, ok: tuple[int, ...] = (0,)) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode not in ok:
            raise GitFailed(args, completed.stderr or completed.stdout)
        return completed.stdout

    def _untracked(self, root: Path) -> tuple[str, ...]:
        raw = self._git("ls-files", "--others", "--exclude-standard", "-z", cwd=root)
        return tuple(chunk for chunk in raw.split("\0") if chunk)

    def _source(self, repo: RepoRef) -> Path:
        path = self._repos.get(repo.slug)
        if path is None:
            raise UnknownRepository(
                f"no local source is configured for {repo.slug!r}. This provider works on "
                f"checkouts it can create, so an unconfigured repository is not an empty "
                f"repository — it is one this provider must not invent. Configured: "
                f"{sorted(self._repos)}"
            )
        return path

    def _worktree_path(self, session_id: SessionId) -> Path:
        # The session id is sanitised rather than used raw: it names a directory,
        # and an id containing a separator would place the checkout outside the
        # root the provider owns.
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(session_id))
        return (self._worktree_root / safe).resolve()

    # -- Workspace ---------------------------------------------------------
    async def prepare(
        self, repo: RepoRef, base_ref: str, session_id: SessionId
    ) -> WorkspaceHandle:
        existing = self._handles.get(session_id)
        if existing is not None:
            return existing

        source = self._source(repo)
        path = self._worktree_path(session_id)
        branch = f"{self._branch_prefix}/{session_id}"

        if path.exists():
            # Registered from an earlier run. Reuse it rather than failing: a
            # resumed session must land in the tree its earlier work is in.
            self._git("worktree", "repair", str(path), cwd=source)
        else:
            self._git(
                "worktree", "add", "-b", branch, str(path), base_ref, cwd=source
            )

        handle = WorkspaceHandle(
            session_id=session_id,
            repo=repo,
            base_ref=base_ref,
            branch=branch,
            root=path,
            is_ephemeral=False,
        )
        self._handles[session_id] = handle
        return handle

    async def diff(self, handle: WorkspaceHandle) -> UnifiedDiff:
        """The working tree against the commit this session started from.

        ``git diff`` describes tracked changes; **untracked files are added
        separately**, because ``git diff`` says nothing at all about them. A file
        the agent created is the change — and adding a file is the shape of most
        first changes, so omitting them made an agent that had just written a new
        module look like an agent that had done nothing.

        The obvious alternative, ``git add --intent-to-add``, is a trap: it makes
        untracked files appear in ``git diff``, and it does so by putting an entry
        in the index pointing at the **empty blob**. A later ``git commit`` would
        then commit an empty file. This approach leaves the index alone.
        """
        sections: list[str] = []
        tracked = self._git("diff", handle.base_ref, cwd=handle.root)
        if tracked:
            sections.append(tracked)

        for relative in self._untracked(handle.root):
            # `--no-index` exits 1 whenever the two sides differ, which here is
            # always — that is a result, not a failure.
            sections.append(
                self._git(
                    "diff",
                    "--no-index",
                    "--",
                    "/dev/null",
                    relative,
                    cwd=handle.root,
                    ok=(0, 1),
                )
            )
        return "".join(sections)

    async def changed_paths(self, handle: WorkspaceHandle) -> Sequence[str]:
        """Including untracked files: a file the agent created is a change."""
        # `-z` rather than newline-separated: a path may contain a newline, and
        # splitting on it would turn one path into two.
        tracked = self._git("diff", "--name-only", "-z", handle.base_ref, cwd=handle.root)
        seen: dict[str, None] = {}
        for chunk in (tracked + "\0".join(self._untracked(handle.root))).split("\0"):
            if chunk:
                seen.setdefault(chunk, None)
        return tuple(seen)

    async def reset(self, handle: WorkspaceHandle) -> None:
        """Return the checkout to the base commit, untracked files included.

        ``--hard`` alone leaves untracked files behind, so an agent's half-written
        new file would survive a reset and appear in the next diff as if it had
        always been part of the work.
        """
        self._git("reset", "--hard", handle.base_ref, cwd=handle.root)
        self._git("clean", "-fd", cwd=handle.root)

    async def dispose(self, handle: WorkspaceHandle) -> None:
        """Remove the checkout *and* unregister it.

        Removing the directory alone leaves the registration behind, and the next
        ``worktree add`` for that path fails with "already registered" — a failure
        that appears much later than its cause.
        """
        source = self._repos.get(handle.repo.slug)
        self._handles.pop(handle.session_id, None)
        if source is None:
            return
        self._git("worktree", "remove", "--force", str(handle.root), cwd=source)
        self._git("worktree", "prune", cwd=source)
