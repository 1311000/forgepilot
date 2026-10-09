"""The git-worktree workspace: one real checkout per session.

`providers/workspace/local.py` says outright that it never clones — it copies
seeded files into a temp directory and calls the result a workspace. That is
honest for a skeleton and useless for a deployment: nothing about the directory
is a repository, so `git status`, a branch, or a commit has nothing to attach to.

This provider gives each session a real ``git worktree`` of a real repository.
The difference is not cosmetic — it is what makes the rest of the delivery loop
(commit, push, a reviewable diff) able to exist at all.

These tests use the real ``git`` binary. Unlike sandbox containment — which
cannot be exercised on this machine — git is present and behaves the same on
both platforms, so there is no reason to test a stub of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forgepilot.seams.refs import RepoRef, SessionId

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")
SLUG = REPO.slug


def git(*args: str, cwd: Path | None = None) -> str:
    """Run git for real, with an identity, so a commit cannot fail on config."""
    completed = subprocess.run(
        ["git", "-c", "user.name=ForgePilot", "-c", "user.email=bot@forgepilot.test", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return completed.stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A real repository, on branch ``main``, with one commit."""
    root = tmp_path / "origin"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    git("init", "-b", "main", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-m", "initial", cwd=root)
    return root


@pytest.fixture
async def workspace(origin: Path, tmp_path: Path):
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin(
            "forgepilot.providers.workspace.git_worktree:GitWorktreeWorkspacePlugin"
        ),
        {"repos": {SLUG: str(origin)}, "worktree_root": str(tmp_path / "worktrees")},
    )
    try:
        yield root.workspace
    finally:
        await dispose_fiber(fiber)


def is_worktree(path: Path) -> bool:
    inside = git("rev-parse", "--is-inside-work-tree", cwd=path).strip()
    return inside == "true"


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------
async def test_prepare_produces_a_real_checkout_of_the_repository(
    workspace, origin: Path, tmp_path: Path
) -> None:
    """Not a directory of copied files — a checkout, in the git sense.

    Everything downstream depends on this: a commit needs an index, a branch
    needs a ref, and a reviewable diff needs a repository to diff against.
    """
    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))

    assert is_worktree(handle.root)
    assert (handle.root / "src" / "app.py").read_text(encoding="utf-8") == (
        "def main():\n    return 1\n"
    ), "the repository's content is not there, so this is not a checkout"
    assert handle.root != origin, "the workspace must not be the repository itself"


async def test_the_session_gets_its_own_branch(workspace) -> None:
    """Two sessions preparing the same repo must not share a working tree.

    That is the whole reason for a worktree rather than a clone: isolated working
    directories over one object store, so a second session does not pay for a
    second copy of the history — and cannot see the first session's edits.
    """
    first = await workspace.prepare(REPO, "main", SessionId("s-1"))
    second = await workspace.prepare(REPO, "main", SessionId("s-2"))

    assert first.root != second.root
    assert first.branch != second.branch
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=first.root).strip() == first.branch
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=second.root).strip() == second.branch


async def test_preparing_twice_for_one_session_does_not_make_a_second_worktree(
    workspace,
) -> None:
    """A resumed session must land in the tree its earlier work is in."""
    first = await workspace.prepare(REPO, "main", SessionId("s-1"))
    again = await workspace.prepare(REPO, "main", SessionId("s-1"))

    assert again.root == first.root


async def test_an_unknown_repository_is_refused(workspace) -> None:
    """A slug nobody configured is not a repository this provider may touch."""
    from forgepilot.seams.workspace import UnknownRepository

    with pytest.raises(UnknownRepository, match="nowhere/at/all"):
        await workspace.prepare(
            RepoRef(host="nowhere", namespace="at", name="all"), "main", SessionId("s-1")
        )


async def test_the_root_is_absolute_and_resolved(workspace) -> None:
    """It becomes the sandbox policy's ``workspace_root``; an unresolved path
    containing ``..`` or a symlink would widen that boundary."""
    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))

    assert handle.root.is_absolute()
    assert handle.root == handle.root.resolve()


# ---------------------------------------------------------------------------
# dispose
# ---------------------------------------------------------------------------
async def test_dispose_removes_the_worktree_and_prunes_its_registration(
    workspace, origin: Path
) -> None:
    """A leftover registration makes the next ``worktree add`` fail, and the
    ``.git`` directory keeps pointing at a tree that is gone."""
    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))

    await workspace.dispose(handle)

    assert not handle.root.exists()
    listed = git("worktree", "list", cwd=origin)
    assert str(handle.root).replace("\\", "/").split("/")[-1] not in listed


# ---------------------------------------------------------------------------
# diff / changed_paths / reset
# ---------------------------------------------------------------------------
async def test_a_modified_file_appears_in_the_diff(workspace) -> None:
    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))

    (handle.root / "src" / "app.py").write_text("def main():\n    return 2\n", encoding="utf-8")

    text = await workspace.diff(handle)

    assert "src/app.py" in text
    assert "-    return 1" in text
    assert "+    return 2" in text


async def test_a_new_file_appears_in_the_diff(workspace) -> None:
    """A file the agent created is the change, not an aside.

    ``git diff`` says nothing about an untracked file, so a report built from it
    would describe a change that added a file as a change that did nothing. Most
    first changes are a new file.
    """
    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))

    (handle.root / "src" / "retry.py").write_text("MAX = 3\n", encoding="utf-8")

    text = await workspace.diff(handle)

    assert "src/retry.py" in text, "a new file was omitted from the diff"
    assert "+MAX = 3" in text


async def test_changed_paths_includes_untracked_files(workspace) -> None:
    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))
    (handle.root / "src" / "app.py").write_text("def main():\n    return 3\n", encoding="utf-8")
    (handle.root / "src" / "brand_new.py").write_text("x = 1\n", encoding="utf-8")

    paths = await workspace.changed_paths(handle)

    assert set(paths) == {"src/app.py", "src/brand_new.py"}


async def test_reset_removes_work_the_session_had_not_committed(workspace) -> None:
    """``--hard`` alone leaves untracked files, so a half-written new file would
    survive a reset and reappear in the next diff as if it were part of the work."""
    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))
    (handle.root / "src" / "app.py").write_text("def main():\n    return 99\n", encoding="utf-8")
    (handle.root / "src" / "half_written.py").write_text("incomplete(", encoding="utf-8")

    await workspace.reset(handle)

    assert (handle.root / "src" / "app.py").read_text(encoding="utf-8") == (
        "def main():\n    return 1\n"
    )
    assert not (handle.root / "src" / "half_written.py").exists()
    assert await workspace.changed_paths(handle) == ()


async def test_two_sessions_do_not_see_each_others_edits(workspace) -> None:
    """The point of a worktree over a shared directory: isolation, not just space."""
    first = await workspace.prepare(REPO, "main", SessionId("s-1"))
    second = await workspace.prepare(REPO, "main", SessionId("s-2"))
    (first.root / "src" / "app.py").write_text("def main():\n    return 7\n", encoding="utf-8")

    assert await workspace.changed_paths(second) == ()
