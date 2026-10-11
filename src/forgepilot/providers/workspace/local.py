"""Temporary-directory workspace — service ``workspace``.

One directory per session, optionally seeded to stand in for a checkout. Nothing
is cloned: the skeleton has no real repositories, and inventing a clone would make
the provider depend on git.

``diff`` computes a **real** unified diff against the state recorded at
``prepare``. It would have been easy to return an empty string — but then an
end-to-end test asserting "the ticket produced a diff" would be asserting on
something the provider invented rather than on what the model actually changed,
which is the difference between a test and a decoration.

The root is **resolved** before it is returned. A workspace root becomes the
``workspace_root`` of the sandbox policy, so an unresolved path containing a
symlink or ``..`` would silently widen the boundary the sandbox enforces.

The temporary root is resolved once, at construction, and ``tempfile``'s last resort
is refused rather than accepted — see ``_resolve_base`` for why the fallback is a real
configuration on a confined host and not a curiosity.
"""

from __future__ import annotations

import difflib
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.refs import CommitAuthor, CommitRef, RepoRef, SessionId
from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.workspace import (
    SERVICE_NAME,
    NotARepository,
    UnifiedDiff,
    WorkspaceHandle,
)

__all__ = ["LocalTempWorkspacePlugin"]


def _resolve_base(configured: str | None) -> Path:
    """Where a throwaway checkout goes: ``base_dir``, then ``FORGEPILOT_TMP``, then temp.

    ⚠️ **The last of those can be the current directory, and that is a real
    configuration rather than a curiosity.** ``tempfile`` tries ``TMPDIR``, ``TEMP``,
    ``TMP``, ``/tmp``, ``/var/tmp``, ``/usr/tmp`` and finally ``os.getcwd()``. On a host
    whose sandbox denies all of them — measured on this development box: every real
    candidate raises ``PermissionError: [Errno 13] Permission denied`` on ``mkstemp``
    and the only creatable one is the repository root — the fallback is what actually
    happens, and this provider turned it into a checkout inside the project.

    That is worse than untidy. A workspace root is the boundary the sandbox enforces
    and the tree the retriever searches, so one created in an arbitrary working
    directory is a checkout that ``git status``, a whole-tree search and a
    ``--workspace .`` run all see as part of the project. Refusing to guess — and naming
    the two ways to say where it should go — is the honest failure.
    """
    for value in (configured, os.environ.get("FORGEPILOT_TMP")):
        if value:
            resolved = Path(value).resolve()
            resolved.mkdir(parents=True, exist_ok=True)
            return resolved

    fallback = Path(tempfile.gettempdir()).resolve()
    if fallback == Path.cwd().resolve():
        raise RuntimeError(
            "no writable temporary directory: `tempfile` fell back to the current "
            f"directory ({fallback}), and a workspace created there would sit inside "
            "whatever tree the caller happens to be standing in. Set `base_dir` on this "
            "provider's row, or the FORGEPILOT_TMP environment variable, to a directory "
            "a workspace may use."
        )
    return fallback


class LocalTempWorkspacePlugin(PluginService):
    """Serves ``workspace`` using one temp directory per session."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        #: Where a fresh checkout goes. Resolved eagerly so a profile that cannot say
        #: where workspaces belong fails at boot rather than mid-task.
        self._base = _resolve_base(cfg.get("base_dir"))

        #: Files written into a fresh workspace, standing in for a checkout.
        self._seed: dict[str, str] = dict(cfg.get("seed") or {})
        self._handles: dict[SessionId, WorkspaceHandle] = {}
        #: The state at `prepare`, used as the diff baseline.
        self._baseline: dict[SessionId, dict[str, str]] = {}

    # -- fixtures ----------------------------------------------------------
    def seed_files(self, files: Mapping[str, str]) -> LocalTempWorkspacePlugin:
        """Add files a fresh workspace starts with."""
        self._seed.update({str(k): str(v) for k, v in files.items()})
        return self

    # -- Workspace ---------------------------------------------------------
    async def prepare(
        self, repo: RepoRef, base_ref: str, session_id: SessionId
    ) -> WorkspaceHandle:
        if session_id in self._handles:
            return self._handles[session_id]

        root = Path(
            tempfile.mkdtemp(
                prefix=f"forgepilot-{session_id}-",
                dir=str(self._base),
            )
        ).resolve()

        for relative, text in self._seed.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

        handle = WorkspaceHandle(
            session_id=session_id,
            repo=repo,
            base_ref=base_ref,
            branch=f"forgepilot/{session_id}",
            root=root,
            is_ephemeral=True,
        )
        self._handles[session_id] = handle
        self._baseline[session_id] = self._snapshot(root)
        return handle

    async def diff(self, handle: WorkspaceHandle) -> UnifiedDiff:
        current = self._snapshot(handle.root)
        baseline = self._baseline.get(handle.session_id, {})
        return _unified_diff(baseline, current)

    async def changed_paths(self, handle: WorkspaceHandle) -> Sequence[str]:
        current = self._snapshot(handle.root)
        baseline = self._baseline.get(handle.session_id, {})
        names = set(current) | set(baseline)
        return tuple(
            sorted(name for name in names if current.get(name) != baseline.get(name))
        )

    async def commit(
        self,
        handle: WorkspaceHandle,
        message: str,
        author: CommitAuthor,
        constraints: ExecutionConstraints,
    ) -> CommitRef:
        """Refused: this workspace is a directory, not a repository.

        Handing back an invented sha would be the worst available answer — it
        looks like delivery, it lands in the session log as evidence, and nothing
        downstream can tell that no commit exists. Use
        :class:`~forgepilot.providers.workspace.git_worktree.GitWorktreeWorkspacePlugin`
        when the work has to become a commit.
        """
        raise NotARepository(
            "this workspace is a temporary directory, not a repository, so it cannot "
            "produce a commit. Its diff and changed_paths are real; its history is not, "
            "because it has none. Configure a workspace provider that checks the code "
            "out of a repository."
        )

    async def restore(
        self, handle: WorkspaceHandle, ref: str, paths: Sequence[str]
    ) -> None:
        """Refused: a directory has no revisions to restore from.

        Best-effort here would be worse than failing. The caller's whole reason
        for calling is that a check is about to decide something, and it believes
        the tests have been pinned. A silent no-op means the agent's version of
        the tests decides the run while the log says otherwise.
        """
        raise NotARepository(
            f"this workspace is a temporary directory, not a repository, so {ref!r} "
            f"names nothing it could restore {len(paths)} path(s) from. Pinning a "
            f"check to a revision needs a workspace that has revisions."
        )

    async def reset(self, handle: WorkspaceHandle) -> None:
        for child in handle.root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        # Reset restores the baseline, so a subsequent diff is against the same
        # starting point rather than against the caller's abandoned attempt.
        for relative, text in self._seed.items():
            target = handle.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    async def dispose(self, handle: WorkspaceHandle) -> None:
        self._handles.pop(handle.session_id, None)
        self._baseline.pop(handle.session_id, None)
        if handle.is_ephemeral:
            shutil.rmtree(handle.root, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _snapshot(root: Path) -> dict[str, str]:
        """Every text file under ``root``, keyed by relative path.

        Binary files are skipped rather than decoded with replacement: a diff of
        mangled bytes is worse than an absent one, and the tools that would
        change them refuse binary content anyway.
        """
        snapshot: dict[str, str] = {}
        if not root.exists():
            return snapshot
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                snapshot[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
        return snapshot


def _unified_diff(baseline: Mapping[str, str], current: Mapping[str, str]) -> str:
    chunks: list[str] = []
    for name in sorted(set(baseline) | set(current)):
        before = baseline.get(name, "")
        after = current.get(name, "")
        if before == after:
            continue
        from_label = f"a/{name}" if name in baseline else "/dev/null"
        to_label = f"b/{name}" if name in current else "/dev/null"
        chunks.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=from_label,
                tofile=to_label,
            )
        )
    return "".join(chunks)
