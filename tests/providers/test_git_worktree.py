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


def writer():
    """Constraints that may write code in the one repository these tests use."""
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import (
        ExecutionConstraints,
        NetworkAccess,
        Operation,
        Principal,
    )

    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset({REPO}),
        permitted_operations=frozenset({Operation.READ_CODE, Operation.WRITE_CODE}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


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


async def test_one_session_two_repositories_gets_two_worktrees_and_two_branches(
    origin: Path, tmp_path: Path
) -> None:
    """**The M13 falsifier.** A ticket that spans two repositories delivers one
    worktree, one branch and one commit *each*.

    Keyed on the session alone, the second `prepare` returned the first
    repository's handle — so the second repository's "tree" was the first
    repository's tree, its "branch" was the first repository's branch, and the
    parameter naming the repository had a reader that ignored it. Nothing failed:
    the run committed one repository twice and reported two deliveries.
    """
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    other_repo = RepoRef(host="git.corp", namespace="team", name="svc-b")
    other = tmp_path / "origin-b"
    (other / "src").mkdir(parents=True)
    (other / "src" / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    git("init", "-b", "main", cwd=other)
    git("add", "-A", cwd=other)
    git("commit", "-m", "initial", cwd=other)

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin(
            "forgepilot.providers.workspace.git_worktree:GitWorktreeWorkspacePlugin"
        ),
        {
            "repos": {SLUG: str(origin), other_repo.slug: str(other)},
            "worktree_root": str(tmp_path / "worktrees"),
        },
    )
    try:
        session_id = SessionId("s-1")
        first = await root.workspace.prepare(REPO, "main", session_id)
        second = await root.workspace.prepare(other_repo, "main", session_id)
    finally:
        await dispose_fiber(fiber)

    assert first.root != second.root, "two repositories shared one working tree"
    assert first.branch != second.branch, "two repositories shared one branch"
    assert second.repo == other_repo
    assert (second.root / "src" / "b.py").is_file(), "not a checkout of svc-b"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=second.root).strip() == second.branch


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


# ---------------------------------------------------------------------------
# commit — the operation that makes the workspace a deliverable
# ---------------------------------------------------------------------------
async def test_commit_records_the_working_tree_as_a_real_commit(workspace, origin) -> None:
    """A real sha, in a real repository, containing the model's change.

    This is the operation that turns a directory of edits into something a
    colleague can review. It used to live on ``vcs``, whose signature
    ``commit(repo, branch, message, author, constraints)`` **takes no path** —
    so it named the repository but not the tree to commit. The mock hid that,
    because it fabricated shas and never touched a tree at all.
    """
    from forgepilot.seams.refs import CommitAuthor

    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))
    (handle.root / "src" / "app.py").write_text("def main():\n    return 42\n", encoding="utf-8")

    ref = await workspace.commit(
        handle,
        "fix: bounded retry",
        CommitAuthor(name="nil", email="nil@example.test"),
        writer(),
    )

    assert ref.sha and len(ref.sha) == 40, "a real commit has a real sha"
    assert ref.repo == REPO
    assert git("log", "-1", "--format=%s", cwd=handle.root).strip() == "fix: bounded retry"
    assert git("show", "--stat", "HEAD", cwd=handle.root).count("src/app.py") >= 1
    # The commit is on the session's own branch, not on the base branch.
    assert git("log", "-1", "--format=%an", cwd=handle.root).strip() == "nil"


async def test_commit_refuses_when_the_caller_may_not_write_code(workspace) -> None:
    """The check that used to live on ``vcs.commit`` must not be lost in the move."""
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import (
        ExecutionConstraints,
        NetworkAccess,
        Operation,
        Principal,
    )

    read_only = ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset({REPO}),
        permitted_operations=frozenset({Operation.READ_CODE}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )
    from forgepilot.seams.refs import CommitAuthor

    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))
    (handle.root / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="write_code"):
        await workspace.commit(
            handle, "sneaky", CommitAuthor(name="x", email="x@y.z"), read_only
        )

    assert git("log", "--format=%s", cwd=handle.root).strip() == "initial", (
        "a commit was written despite the refusal"
    )


async def test_commit_refuses_a_repository_the_caller_cannot_reach(workspace) -> None:
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import (
        ExecutionConstraints,
        NetworkAccess,
        Operation,
        Principal,
    )
    from forgepilot.seams.vcs import CommitAuthor

    elsewhere = ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset(),
        permitted_operations=frozenset(Operation),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )

    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))

    with pytest.raises(PermissionError, match="not permitted"):
        await workspace.commit(
            handle, "x", CommitAuthor(name="x", email="x@y.z"), elsewhere
        )


async def test_a_branch_left_by_an_earlier_run_is_reused_not_fatal(
    workspace, origin, tmp_path
) -> None:
    """⚠️ The branch outlives the worktree.

    A run that was cancelled, that crashed, or whose worktrees were deleted by
    hand leaves the branch behind — and `worktree add -b` then fails outright
    with "a branch named X already exists", so the session can never be resumed.

    Found by running a real task twice. Reusing it is also correct rather than
    merely convenient: the branch name is derived from the session id, so it
    names this session's work and nothing else.
    """
    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))
    # Remove the worktree but leave the branch — what an aborted run leaves, and
    # what `dispose` deliberately does not clean up: the branch is the session's
    # work, and deleting it would destroy uncommitted history.
    git("worktree", "remove", "--force", str(handle.root), cwd=origin)
    assert git("branch", "--list", handle.branch, cwd=origin).strip(), "no branch left to reuse"

    # A **fresh provider**, because that is what a restarted process is. Reusing
    # this one would hit its handle cache and never reach `git worktree add`.
    provider = await fresh_workspace(origin, tmp_path)
    again = await provider.prepare(REPO, "main", SessionId("s-1"))

    assert again.root.exists()
    assert again.branch == handle.branch

    # And the state a cleanup job leaves: **the directory gone, the registration
    # still there**. `worktree add` refuses that outright — "a missing but already
    # registered worktree" — so this is a third state, not a variant of the two
    # above, and it was found by running the same task a third time.
    import shutil

    shutil.rmtree(again.root, ignore_errors=True)
    third = await fresh_workspace(origin, tmp_path)
    repaired = await third.prepare(REPO, "main", SessionId("s-1"))

    assert repaired.root.exists(), "a stale registration blocked the worktree"


async def fresh_workspace(origin: Path, tmp_path: Path):
    """A newly loaded provider over the same repository, as after a restart."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root

    root = create_root()
    await root.plugin(
        resolve_plugin(
            "forgepilot.providers.workspace.git_worktree:GitWorktreeWorkspacePlugin"
        ),
        {"repos": {SLUG: str(origin)}, "worktree_root": str(tmp_path / "worktrees")},
    )
    return root.workspace


# ---------------------------------------------------------------------------
# restore — pinning a path to a revision
# ---------------------------------------------------------------------------
async def test_restore_puts_a_modified_file_back_to_the_ref(workspace) -> None:
    """The mechanism behind "the checks ran against the project's own tests".

    An agent that cannot make a check pass can make the check stop testing the
    thing. Restoring is what turns that from a green run into a red one.
    """
    handle = await workspace.prepare(REPO, "main", SessionId("s-restore"))
    (handle.root / "src" / "app.py").write_text("def main():\n    return 'weakened'\n", encoding="utf-8")

    await workspace.restore(handle, "main", ["src/app.py"])

    assert (handle.root / "src" / "app.py").read_text(encoding="utf-8") == (
        "def main():\n    return 1\n"
    )


async def test_restore_deletes_a_file_the_ref_never_had(workspace) -> None:
    """The other half, and the easier one to forget.

    A test the agent *added* has nothing to restore to, so `checkout` cannot
    remove it — and leaving it in place means it keeps taking part in a run it
    was supposed to be excluded from. Deleting the whole test file is the
    cheapest way to make a suite pass and leaves the least trace in a diff.
    """
    handle = await workspace.prepare(REPO, "main", SessionId("s-delete"))
    created = handle.root / "src" / "test_new.py"
    created.write_text("def test_nothing():\n    pass\n", encoding="utf-8")
    assert created.exists()

    await workspace.restore(handle, "main", ["src/test_new.py"])

    assert not created.exists(), "a file the base revision never had survived the pin"


async def test_restore_leaves_paths_it_was_not_asked_about_alone(workspace) -> None:
    """Pinning is a scalpel. Restoring everything would run the checks against
    code that does not contain the fix."""
    handle = await workspace.prepare(REPO, "main", SessionId("s-scoped"))
    (handle.root / "src" / "app.py").write_text("the fix\n", encoding="utf-8")
    (handle.root / "src" / "test_other.py").write_text("def test_x():\n    pass\n", encoding="utf-8")

    await workspace.restore(handle, "main", ["src/test_other.py"])

    assert (handle.root / "src" / "app.py").read_text(encoding="utf-8") == "the fix\n"
    assert not (handle.root / "src" / "test_other.py").exists()
