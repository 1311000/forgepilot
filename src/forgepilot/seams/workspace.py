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

from forgepilot.errors import ForgePilotError
from forgepilot.seams.refs import CommitAuthor, CommitRef, RepoRef, SessionId
from forgepilot.seams.security import ExecutionConstraints


class NotARepository(ForgePilotError):
    """This workspace is a directory, and the caller asked it to be a repository.

    Refused rather than answered with a made-up sha, which is the worst of the
    available options: it looks like delivery, it lands in the session log as
    evidence, and nothing downstream can tell that no commit exists.
    """


class NothingToCommit(ForgePilotError):
    """The working tree is unchanged, so there is nothing to record.

    Reachable without anything being wrong with the harness: an agent whose write
    was refused, or a ticket describing something already fixed. It surfaced as
    git's own *"nothing to commit, working tree clean"* wrapped in a `GitFailed`
    from the last step of a run that had otherwise succeeded — a confusing way to
    say "no change was produced", and the caller cannot act on it.
    """


class UnknownRepository(ForgePilotError):
    """A repository the provider has no local source for.

    Raised rather than creating an empty checkout: a workspace that silently
    contains nothing is indistinguishable from a repository that is genuinely
    empty, and every diff taken against it would look like the agent deleted the
    whole codebase.
    """


__all__ = [
    "SERVICE_NAME",
    "NotARepository",
    "UnknownRepository",
    "NothingToCommit",
    "WorkspaceHandle",
    "UnifiedDiff",
    "Workspace",
]

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

    async def commit(
        self,
        handle: WorkspaceHandle,
        message: str,
        author: CommitAuthor,
        constraints: ExecutionConstraints,
    ) -> CommitRef:
        """Record the working tree as a commit on the session's branch.

        ⚠️ **This is the one ``Workspace`` method that takes constraints**, and
        the asymmetry is deliberate. The others read or rearrange a working
        directory; this one writes to version-control history, which is the
        artefact a reviewer sees. It used to live on ``vcs`` — whose signature
        ``commit(repo, branch, message, author, constraints)`` names the
        repository but **not the tree to commit**, which is why it could only ever
        be satisfied by a provider that keeps the two implicitly connected.

        A provider that is not a repository must refuse rather than fabricate a
        sha.
        """
        ...

    async def restore(self, handle: WorkspaceHandle, ref: str, paths: Sequence[str]) -> None:
        """Make ``paths`` in the working tree match ``ref``.

        A path ``ref`` does not contain is **removed**, not skipped: "the agent
        created a new test file" and "the agent weakened an existing one" have to
        be undone the same way, or the newer one survives as an untracked file and
        keeps being collected.

        This exists for one caller — the harness pinning a project's tests to the
        revision the checks are supposed to be judging against. It is not a tool
        and no model may reach it; it restores *away* the agent's work by
        definition, which is exactly what makes it dangerous to expose.

        A provider that is not a repository must refuse rather than leave the
        working tree in a state the caller believes it changed.
        """
        ...

    async def reset(self, handle: WorkspaceHandle) -> None: ...

    async def dispose(self, handle: WorkspaceHandle) -> None: ...
