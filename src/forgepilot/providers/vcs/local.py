"""A repository with no host behind it — service ``vcs``.

After P1-2 moved ``create_branch`` and ``commit`` onto the workspace seam, what
remains on ``vcs`` is exactly what needs a **remote**: mirroring, pushing, and
opening pull requests. Every one of those needs a service that owns the
repository, and this provider exists for deployments that do not have one.

It is not a stub. ``MockVcsPlugin`` declares ``pull_requests`` and answers with a
fabricated reference — which is right for a test double and wrong for a
deployment, because the task then reports ``DELIVERED`` on the strength of a PR
that does not exist. This provider declares what it can actually do, so the task
ends at :attr:`TaskState.READY`: the work is committed on a local branch and the
draft is written, and nothing claims to have been published.

That is the honest terminal state, and it is the one a team without a GitLab or
GitHub integration would actually see.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.refs import RepoRef
from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.vcs import (
    SERVICE_NAME,
    ChangedFile,
    DiffBundle,
    PullRequestDraft,
    PullRequestRef,
)

__all__ = ["LocalRepositoryVcsPlugin", "NoRepoHost"]


class NoRepoHost(RuntimeError):
    """The operation needs a service that owns the repository, and there is none.

    Raised rather than answered: a fabricated pull-request reference would look
    like delivery, would land in the session log as evidence, and nothing
    downstream could tell that no pull request exists.
    """


class LocalRepositoryVcsPlugin(PluginService):
    """Serves ``vcs`` for local checkouts, with no remote.

    Config::

        repos:  {"git.local/demo/stats": "/srv/checkouts/stats"}
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._repos: dict[str, Path] = {
            str(slug): Path(path).resolve() for slug, path in (cfg.get("repos") or {}).items()
        }
        #: Refusals, so a test can assert one happened rather than infer it.
        self.denied_calls = 0

    def capabilities(self) -> frozenset[str]:
        """What this provider can do. Note what is **absent**.

        No ``push`` and no ``pull_requests``: both need a remote. Declaring them
        would make `run_task` call them, and there is nothing to call. The
        capability list *is* the honest answer to "can this deployment deliver",
        which is why `run_task` consults it rather than assuming.
        """
        return frozenset({"diff", "changed_files"})

    def _authorise(self, repo: RepoRef, constraints: ExecutionConstraints) -> None:
        if not constraints.allows_repo(repo):
            self.denied_calls += 1
            raise PermissionError(f"repo {repo.slug!r} is not permitted")

    def _path(self, repo: RepoRef, constraints: ExecutionConstraints) -> Path:
        self._authorise(repo, constraints)
        path = self._repos.get(repo.slug)
        if path is None:
            self.denied_calls += 1
            raise FileNotFoundError(
                f"no local checkout is configured for {repo.slug!r}. Configured: "
                f"{sorted(self._repos)}"
            )
        return path

    async def resolve(self, slug: str) -> RepoRef:
        parts = slug.split("/")
        if len(parts) != 3:
            raise ValueError(f"expected 'host/namespace/name', got {slug!r}")
        return RepoRef(host=parts[0], namespace=parts[1], name=parts[2])

    async def ensure_mirror(self, repo: RepoRef, constraints: ExecutionConstraints) -> Path:
        """The local checkout *is* the mirror. Nothing is fetched."""
        return self._path(repo, constraints)

    async def push(self, repo: RepoRef, branch: str, constraints: ExecutionConstraints) -> None:
        self._path(repo, constraints)  # still checks permission, and still fails
        raise NoRepoHost(
            f"cannot push {branch!r} to {repo.slug!r}: no repo host is configured. "
            f"Pushing needs a service that owns the repository."
        )

    async def open_pull_request(
        self, draft: PullRequestDraft, constraints: ExecutionConstraints
    ) -> PullRequestRef:
        self._path(draft.repo, constraints)
        raise NoRepoHost(
            f"cannot open a pull request against {draft.repo.slug!r}: no repo host is "
            f"configured. A pull request is an object that service owns — with a number "
            f"it assigns — so there is nothing here to return and nothing that could be "
            f"invented without lying."
        )

    async def update_pull_request_body(
        self, pull_request: PullRequestRef, body_md: str, constraints: ExecutionConstraints
    ) -> None:
        self._path(pull_request.repo, constraints)
        raise NoRepoHost(
            f"cannot edit the description of {pull_request.repo.slug!r}#"
            f"{pull_request.number}: no repo host is configured. The pull request "
            f"does not exist here either, so there is nothing to edit."
        )

    async def diff(
        self, repo: RepoRef, base: str, head: str, constraints: ExecutionConstraints
    ) -> DiffBundle:
        import subprocess

        path = self._path(repo, constraints)
        completed = subprocess.run(
            ["git", "diff", base, head],
            cwd=str(path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return DiffBundle(
            base_ref=base, head_ref=head, unified_diff=completed.stdout or ""
        )

    async def changed_files(
        self, repo: RepoRef, base: str, head: str, constraints: ExecutionConstraints
    ) -> Sequence[ChangedFile]:
        import subprocess

        path = self._path(repo, constraints)
        completed = subprocess.run(
            ["git", "diff", "--numstat", base, head],
            cwd=str(path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        out: list[ChangedFile] = []
        for line in (completed.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, name = parts
            out.append(
                ChangedFile(
                    path=name,
                    additions=int(added) if added.isdigit() else 0,
                    deletions=int(removed) if removed.isdigit() else 0,
                    is_binary=not added.isdigit(),
                )
            )
        return tuple(out)
