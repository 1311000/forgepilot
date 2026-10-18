"""Version-control / repo-host seam — service name ``vcs``.

Ships against a mock first (local bare repos under ``fixtures/``). Real hosts
arrive as providers behind this same interface.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from forgepilot.seams.refs import CommitAuthor, RepoRef
from forgepilot.seams.security import ExecutionConstraints

__all__ = [
    "SERVICE_NAME",
    "CommitAuthor",
    "ChangedFile",
    "DiffBundle",
    "PullRequestDraft",
    "PullRequestRef",
    "Vcs",
]

SERVICE_NAME = "vcs"


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    additions: int = 0
    deletions: int = 0
    is_binary: bool = False


@dataclass(frozen=True, slots=True)
class DiffBundle:
    """A unified diff plus the metadata a review needs."""

    base_ref: str
    head_ref: str
    unified_diff: str
    files: tuple[ChangedFile, ...] = ()
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class PullRequestDraft:
    repo: RepoRef
    source_branch: str
    target_branch: str
    title: str
    body_md: str
    #: The ticket this change answers, so the PR is traceable back to the work
    #: item rather than only to a branch name.
    ticket_id: str | None = None
    draft: bool = True


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    repo: RepoRef
    number: int
    url: str = ""


@runtime_checkable
class Vcs(Protocol):
    """Repository access, with authority passed in on every effectful call.

    Every method except ``resolve`` (which only parses a slug and touches
    nothing) takes ``constraints``. This is deliberate and consistent with
    :mod:`forgepilot.seams.retriever`: the seam re-checks authority itself rather
    than trusting that its caller did. A single place where the check is
    "someone else's job" is a place where one caller eventually forgets.
    """

    def capabilities(self) -> frozenset[str]: ...

    async def resolve(self, slug: str) -> RepoRef:
        """Parse ``host/namespace/name``. Reads nothing, so needs no authority."""
        ...

    async def ensure_mirror(self, repo: RepoRef, constraints: ExecutionConstraints) -> Path: ...

    async def push(self, repo: RepoRef, branch: str, constraints: ExecutionConstraints) -> None: ...

    async def open_pull_request(
        self, draft: PullRequestDraft, constraints: ExecutionConstraints
    ) -> PullRequestRef: ...

    async def update_pull_request_body(
        self, pull_request: PullRequestRef, body_md: str, constraints: ExecutionConstraints
    ) -> None:
        """Replace the description of a pull request this run opened.

        Exists for exactly one reason, and it is a fact about serial delivery: a
        plural ticket's pull requests are opened one after another, so the first
        one is created before the run knows how the second ends. Its body says so
        at the time — "not attempted yet" — and that sentence stops being the
        whole truth the moment the next delivery succeeds. A reviewer reading the
        first pull request should not have to know the run order to find out where
        the sibling stands, so the sibling sections are rewritten once every
        delivery has a verdict.

        ``constraints`` is checked like every other effectful call: editing a pull
        request is publishing, so it needs the same operation that opening one did.
        """
        ...

    async def diff(
        self, repo: RepoRef, base: str, head: str, constraints: ExecutionConstraints
    ) -> DiffBundle: ...

    async def changed_files(
        self, repo: RepoRef, base: str, head: str, constraints: ExecutionConstraints
    ) -> Sequence[ChangedFile]: ...
