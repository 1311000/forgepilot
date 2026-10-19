"""A workspace that can also put every repository of a ticket in **one** directory.

Why this exists, in one sentence: a check that runs inside one repository's tree
cannot see its neighbour, so a change split across two repositories that only
breaks *in combination* is verified by two passes for a combination nobody ran.

The layout is deliberately the boring one — one directory per repository, named
by the repository's last slug component, beside each other::

    <worktree_root>/merged-<session>-checks/
        stats/
        reporting/

so a check can put the directory on ``PYTHONPATH`` (or ``cd`` into a package) and
import across the two. Nothing is merged into a single tree: the repositories are
still separate checkouts, and a path belongs to exactly one of them.

``.git`` is left out. The check needs files, not history, and copying a git
worktree's administrative files would make the copy look like a checkout of a
repository whose index points at another directory. Everything else is copied.

This is a **subclass**, not a wrapper around another provider: a wrapper would
have to re-provide the ``workspace`` service, and Cordis registers a provide at
construction, so two instances would both claim the name.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from forgepilot.providers.workspace.git_worktree import GitWorktreeWorkspacePlugin
from forgepilot.seams.refs import RepoRef
from forgepilot.seams.workspace import SERVICE_NAME

__all__ = ["MergedCheckoutWorkspacePlugin"]


class MergedCheckoutWorkspacePlugin(GitWorktreeWorkspacePlugin):
    """Serves ``workspace`` like the git worktree provider, plus a merged tree.

    Config is the parent's, unchanged: ``repos``, ``worktree_root``,
    ``branch_prefix``. The merged tree is built on demand by task mode, from the
    worktrees this provider already handed out — it never checks anything out a
    second time, so what the checks see is the same bytes the per-repository
    checks saw.
    """

    provide = SERVICE_NAME

    async def merged_checkout(
        self,
        trees: Sequence[tuple[RepoRef, Path]],
        *,
        session_id: Any,
        name: str = "checks",
    ) -> Path:
        """Copy ``trees`` side by side under the provider's worktree root.

        ``name`` distinguishes two merged trees of one session (a check and a
        reviewer's reproduction, say). The directory is removed first if it is
        there: a stale copy from an earlier attempt is worse than no copy, because
        it passes for the current one.
        """
        if not trees:
            raise ValueError("a merged checkout needs at least one repository tree")

        destination = self._worktree_root / f"merged-{session_id}-{name}"
        if destination.exists():
            shutil.rmtree(destination)

        seen: dict[str, str] = {}
        for repo, tree in trees:
            directory = repo.name
            if directory in seen:
                # Two repositories named `api` in different namespaces are common
                # enough, and a silent last-one-wins copy would make the check run
                # against one of them with no way to tell which.
                raise ValueError(
                    f"cannot merge {seen[directory]!r} and {repo.slug!r} into one "
                    f"directory: both are named {directory!r}"
                )
            seen[directory] = repo.slug
            shutil.copytree(
                tree,
                destination / directory,
                ignore=shutil.ignore_patterns(".git"),
            )

        return destination

    def dispose_merged(self, root: Path) -> None:
        """Remove a merged tree. Best-effort: a failed cleanup is not a delivery."""
        shutil.rmtree(root, ignore_errors=True)
