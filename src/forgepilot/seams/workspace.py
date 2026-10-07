"""Workspace seam — service name ``workspace``.

A workspace is the one place a session may modify files. Its root becomes the
``workspace_root`` of every sandbox policy for that session, so the location of a
workspace and the boundary of the sandbox are the same fact stated twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from forgepilot.seams.refs import RepoRef, SessionId

__all__ = ["SERVICE_NAME", "WorkspaceHandle", "UnifiedDiff", "Workspace"]

SERVICE_NAME = "workspace"


@dataclass(frozen=True, slots=True)
class WorkspaceHandle:
    session_id: SessionId
    repo: RepoRef
    base_ref: str
    branch: str
    #: Canonicalised absolute path. Must be resolved before it is handed to a
    #: sandbox policy, or a symlinked ``..`` can widen the boundary.
    root: Path
    is_ephemeral: bool = True


UnifiedDiff = str


@runtime_checkable
class Workspace(Protocol):
    async def prepare(
        self, repo: RepoRef, base_ref: str, session_id: SessionId
    ) -> WorkspaceHandle: ...

    async def diff(self, handle: WorkspaceHandle) -> UnifiedDiff: ...

    async def changed_paths(self, handle: WorkspaceHandle) -> Sequence[str]: ...

    async def reset(self, handle: WorkspaceHandle) -> None: ...

    async def dispose(self, handle: WorkspaceHandle) -> None: ...
