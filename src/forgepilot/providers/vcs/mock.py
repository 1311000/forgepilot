"""In-memory VCS — service ``vcs``.

Records every branch, commit and pull request in memory so a task-mode test can
assert on what the runtime *did* — "it opened exactly one PR against the right
ticket" — without a git binary or a network.

Authority is checked here rather than deferred to the caller: each method
requires the operation it actually performs, so a permissions bug surfaces in the
skeleton instead of in production. The real provider will additionally rely on
the host's own enforcement, but "the host will stop us" is not a substitute for
checking.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.refs import BranchRef, CommitRef, RepoRef
from forgepilot.seams.security import ExecutionConstraints, Operation
from forgepilot.seams.vcs import (
    SERVICE_NAME,
    ChangedFile,
    CommitAuthor,
    DiffBundle,
    PullRequestDraft,
    PullRequestRef,
)

__all__ = ["MockVcsPlugin"]


class MockVcsPlugin(PluginService):
    """Serves ``vcs`` from in-memory state."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._mirror_root = Path(cfg.get("mirror_root", "fixtures/repos"))
        self._commits: list[tuple[RepoRef, str, str, CommitAuthor]] = []
        self._branches: dict[tuple[RepoRef, str], BranchRef] = {}
        self._pushes: list[tuple[RepoRef, str]] = []
        self._pull_requests: list[PullRequestDraft] = []
        self._updated: list[tuple[PullRequestRef, str]] = []
        self._diffs: dict[tuple[RepoRef, str, str], DiffBundle] = {}
        self._seq = 0
        #: Calls refused for lack of authority, so a test can assert that a
        #: refusal happened rather than inferring it from an absence.
        self.denied_calls: int = 0

    # -- fixtures ----------------------------------------------------------
    def set_diff(self, repo: RepoRef, base: str, head: str, diff: DiffBundle) -> MockVcsPlugin:
        self._diffs[(repo, base, head)] = diff
        return self

    @property
    def pull_requests(self) -> tuple[PullRequestDraft, ...]:
        """Everything opened, in order. The task-mode acceptance test reads this."""
        return tuple(self._pull_requests)

    @property
    def pull_request_bodies(self) -> tuple[tuple[PullRequestRef, str], ...]:
        """Bodies rewritten after opening, in order.

        A plural ticket's pull requests are opened serially, so the first body is
        written before the sibling has a verdict. This is what a test reads to
        assert the rewrite actually happened - the draft alone cannot show it,
        because the draft is what was sent *before* the rewrite.
        """
        return tuple(self._updated)

    @property
    def commits(self) -> tuple[tuple[RepoRef, str, str, CommitAuthor], ...]:
        return tuple(self._commits)

    @property
    def pushes(self) -> tuple[tuple[RepoRef, str], ...]:
        """Everything pushed, in order.

        Exposed for the same reason `pull_requests` is: a test asserting that
        something was *not* published has to be able to look. Asserting on the
        absence of a pull request alone would not catch a branch that went out
        anyway — and pushing is the half of publishing that a human approving
        the pull request has implicitly approved too.
        """
        return tuple(self._pushes)

    def _next_sha(self) -> str:
        self._seq += 1
        return f"{self._seq:040x}"

    # -- authority ---------------------------------------------------------
    def _authorise(
        self, repo: RepoRef, constraints: ExecutionConstraints, operation: Operation
    ) -> None:
        if not constraints.allows_repo(repo):
            self.denied_calls += 1
            raise PermissionError(f"repo {repo.slug!r} is not permitted")
        if not constraints.allows_operation(operation):
            self.denied_calls += 1
            raise PermissionError(
                f"principal {constraints.principal_id!r} may not {operation.value} on {repo.slug!r}"
            )

    # -- Vcs ---------------------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        # `push` was missing here while `push` was implemented below — an
        # omission nobody could notice while nothing read this list. It is read
        # now, and the effect was that the mock took the no-host path and every
        # task ended at READY instead of DELIVERED.
        return frozenset({"branches", "commits", "push", "pull_requests", "diff"})

    async def resolve(self, slug: str) -> RepoRef:
        parts = slug.split("/")
        if len(parts) != 3:
            raise ValueError(f"expected 'host/namespace/name', got {slug!r}")
        return RepoRef(host=parts[0], namespace=parts[1], name=parts[2])

    async def ensure_mirror(self, repo: RepoRef, constraints: ExecutionConstraints) -> Path:
        self._authorise(repo, constraints, Operation.READ_CODE)
        # Stands in for a bare mirror. Never touches the filesystem: the mock
        # must not create state a test did not ask for.
        return self._mirror_root / repo.host / repo.namespace / repo.name

    async def push(self, repo: RepoRef, branch: str, constraints: ExecutionConstraints) -> None:
        """Publish a branch to the host.

        It used to refuse a branch it had not seen created — an artefact of the
        old flow, where ``vcs`` owned branch creation and so could assume it had
        been told. A push is precisely how a remote *learns* about a branch that
        exists locally, so requiring prior knowledge modelled the wrong thing.
        The branch is now created by the working tree, which is where it exists.
        """
        self._authorise(repo, constraints, Operation.WRITE_CODE)
        self._pushes.append((repo, branch))
        self._branches.setdefault(
            (repo, branch),
            BranchRef(repo=repo, name=branch, head=CommitRef(repo=repo, sha=self._next_sha())),
        )

    async def open_pull_request(
        self, draft: PullRequestDraft, constraints: ExecutionConstraints
    ) -> PullRequestRef:
        self._authorise(draft.repo, constraints, Operation.OPEN_PR)
        self._pull_requests.append(draft)
        number = len(self._pull_requests)
        return PullRequestRef(
            repo=draft.repo, number=number, url=f"mock://{draft.repo.slug}/pr/{number}"
        )

    async def update_pull_request_body(
        self, pull_request: PullRequestRef, body_md: str, constraints: ExecutionConstraints
    ) -> None:
        self._authorise(pull_request.repo, constraints, Operation.OPEN_PR)
        self._updated.append((pull_request, body_md))

    async def diff(
        self, repo: RepoRef, base: str, head: str, constraints: ExecutionConstraints
    ) -> DiffBundle:
        self._authorise(repo, constraints, Operation.READ_CODE)
        return self._diffs.get(
            (repo, base, head), DiffBundle(base_ref=base, head_ref=head, unified_diff="")
        )

    async def changed_files(
        self, repo: RepoRef, base: str, head: str, constraints: ExecutionConstraints
    ) -> Sequence[ChangedFile]:
        return (await self.diff(repo, base, head, constraints)).files
