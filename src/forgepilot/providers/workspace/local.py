"""Temporary-directory workspace — service ``workspace``.

One directory per session, under a caller-supplied root. Nothing is cloned: the
skeleton has no real repositories to clone from, and inventing a clone would make
the mock depend on git.

The root is **resolved** before it is returned. A workspace root becomes the
``workspace_root`` of the sandbox policy, so an unresolved path containing a
symlink or ``..`` would silently widen the boundary the sandbox enforces.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.refs import RepoRef, SessionId
from forgepilot.seams.workspace import SERVICE_NAME, UnifiedDiff, WorkspaceHandle

__all__ = ["LocalTempWorkspacePlugin"]


class LocalTempWorkspacePlugin(PluginService):
    """Serves ``workspace`` using one temp directory per session."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        base = cfg.get("base_dir")
        self._base = Path(base).resolve() if base else None
        if self._base is not None:
            self._base.mkdir(parents=True, exist_ok=True)
        self._handles: dict[SessionId, WorkspaceHandle] = {}

    async def prepare(
        self, repo: RepoRef, base_ref: str, session_id: SessionId
    ) -> WorkspaceHandle:
        if session_id in self._handles:
            return self._handles[session_id]

        root = Path(
            tempfile.mkdtemp(
                prefix=f"forgepilot-{session_id}-",
                dir=str(self._base) if self._base else None,
            )
        ).resolve()

        handle = WorkspaceHandle(
            session_id=session_id,
            repo=repo,
            base_ref=base_ref,
            branch=f"forgepilot/{session_id}",
            root=root,
            is_ephemeral=True,
        )
        self._handles[session_id] = handle
        return handle

    async def diff(self, handle: WorkspaceHandle) -> UnifiedDiff:
        # A fixture workspace has no history to diff against; the mock VCS owns
        # diffs. Returning an empty diff is honest, whereas fabricating one would
        # make a review-mode test pass on invented data.
        return ""

    async def changed_paths(self, handle: WorkspaceHandle) -> Sequence[str]:
        return ()

    async def reset(self, handle: WorkspaceHandle) -> None:
        for child in handle.root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    async def dispose(self, handle: WorkspaceHandle) -> None:
        self._handles.pop(handle.session_id, None)
        if handle.is_ephemeral:
            shutil.rmtree(handle.root, ignore_errors=True)
