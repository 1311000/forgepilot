"""The merged checkout: several repositories, one directory, one check.

These are provider tests: they build the tree the cross-repository check runs in,
and they assert the properties that make that check trustworthy — the bytes come
from the trees the per-repository checks verified, a name clash is refused rather
than resolved silently, and a stale copy never stands in for the current one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forgepilot.seams.refs import RepoRef
from forgepilot.seams.session import SessionId

A = RepoRef(host="git.corp", namespace="team", name="stats")
B = RepoRef(host="git.corp", namespace="team", name="reporting")


def git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@test", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def a_repo(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git("init", "-b", "main", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-m", "initial", cwd=root)
    return root


@pytest.fixture
async def workspace(tmp_path: Path):
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin(
            "forgepilot.providers.workspace.merged:MergedCheckoutWorkspacePlugin"
        ),
        {"repos": {}, "worktree_root": str(tmp_path / "worktrees")},
    )
    try:
        yield root.workspace
    finally:
        await dispose_fiber(fiber)


@pytest.fixture
def two_trees(tmp_path: Path) -> list[tuple[RepoRef, Path]]:
    return [
        (A, a_repo(tmp_path / "stats", {"src/stats.py": "VALUE = 1\n"})),
        (B, a_repo(tmp_path / "reporting", {"src/report.py": "from stats import VALUE\n"})),
    ]


async def test_the_tree_holds_each_repository_side_by_side(workspace, two_trees) -> None:
    merged = await workspace.merged_checkout(two_trees, session_id=SessionId("s-1"))

    # Directory names are the last slug component: a check imports `stats` by the
    # name it has in the repository, not by the full slug.
    assert (merged / "stats" / "src" / "stats.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (merged / "reporting" / "src" / "report.py").is_file()
    # A file in one repository is not in the other: the two trees stay separate.
    assert not (merged / "stats" / "src" / "report.py").exists()


async def test_the_copy_carries_the_worktree_bytes_not_a_new_checkout(workspace, two_trees) -> None:
    """The check must see what the per-repository check saw — including uncommitted work."""
    (two_trees[0][1] / "src" / "stats.py").write_text("VALUE = 2\n", encoding="utf-8")

    merged = await workspace.merged_checkout(two_trees, session_id=SessionId("s-1"))

    assert (merged / "stats" / "src" / "stats.py").read_text(encoding="utf-8") == "VALUE = 2\n"


async def test_git_is_not_copied(workspace, two_trees) -> None:
    merged = await workspace.merged_checkout(two_trees, session_id=SessionId("s-1"))

    assert not (merged / "stats" / ".git").exists()


async def test_two_repositories_with_one_name_are_refused(workspace, tmp_path, two_trees) -> None:
    other = RepoRef(host="git.other", namespace="x", name="stats")
    tree = a_repo(tmp_path / "other-stats", {"README.md": "other\n"})

    with pytest.raises(ValueError, match="both are named"):
        await workspace.merged_checkout([two_trees[0], (other, tree)], session_id=SessionId("s-1"))


async def test_a_stale_copy_is_replaced_rather_than_reused(workspace, two_trees) -> None:
    """A leftover directory from an earlier attempt would pass for the current one."""
    first = await workspace.merged_checkout(two_trees, session_id=SessionId("s-1"))
    (first / "stats" / "stale.txt").write_text("left over\n", encoding="utf-8")

    second = await workspace.merged_checkout(two_trees, session_id=SessionId("s-1"))

    assert second == first
    assert not (second / "stats" / "stale.txt").exists()


async def test_dispose_removes_the_tree(workspace, two_trees) -> None:
    merged = await workspace.merged_checkout(two_trees, session_id=SessionId("s-1"))

    workspace.dispose_merged(merged)

    assert not merged.exists()
