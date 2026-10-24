"""A commit turned into an update: renames, deletes, binaries, and no changes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forgepilot.runtime.index_update import (
    GitRefused,
    git_delta,
    git_head,
    plan,
    plan_full,
)
from forgepilot.seams.refs import RepoRef

REPO = RepoRef("git.local", "demo", "delta")


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done.stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "delta"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "delta@example.test")
    _git(root, "config", "user.name", "delta")
    (root / "a.py").write_text("A = 1\n", encoding="utf-8")
    (root / "b.py").write_text("B = 1\n", encoding="utf-8")
    _commit(root, "two files")
    return root


def test_an_added_and_a_modified_file_are_documents_at_the_new_ref(repo: Path) -> None:
    base = git_head(repo)
    (repo / "a.py").write_text("A = 2\n", encoding="utf-8")
    (repo / "c.py").write_text("C = 1\n", encoding="utf-8")
    head = _commit(repo, "modify and add")

    planned = plan(repo, repo=REPO, base=base, head=head)

    assert planned.update.ref == head
    assert planned.update.complete is False
    assert sorted(document.path for document in planned.update.documents) == ["a.py", "c.py"]
    assert planned.update.deletes == ()
    assert planned.changes == 2


def test_a_rename_deletes_the_old_path_and_adds_the_new_one(repo: Path) -> None:
    base = git_head(repo)
    _git(repo, "mv", "a.py", "moved.py")
    head = _commit(repo, "rename")

    planned = plan(repo, repo=REPO, base=base, head=head)

    assert planned.update.deletes == ("a.py",)
    assert [document.path for document in planned.update.documents] == ["moved.py"]


def test_a_deleted_file_is_a_deletion_not_an_empty_document(repo: Path) -> None:
    base = git_head(repo)
    (repo / "a.py").unlink()
    head = _commit(repo, "delete")

    planned = plan(repo, repo=REPO, base=base, head=head)

    assert planned.update.deletes == ("a.py",)
    assert planned.update.documents == ()


def test_an_unreadable_file_is_dropped_and_named(repo: Path) -> None:
    base = git_head(repo)
    (repo / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    head = _commit(repo, "binary")

    planned = plan(repo, repo=REPO, base=base, head=head)

    assert planned.update.documents == ()
    assert len(planned.skipped) == 1
    assert "blob.bin" in planned.skipped[0]


def test_a_file_over_the_limit_is_dropped_and_named(repo: Path) -> None:
    base = git_head(repo)
    (repo / "big.py").write_text("x = 1\n" * 100, encoding="utf-8")
    head = _commit(repo, "big")

    planned = plan(repo, repo=REPO, base=base, head=head, max_bytes=64)

    assert planned.update.documents == ()
    assert "big.py" in planned.skipped[0]
    assert "over 64" in planned.skipped[0]


def test_nothing_changed_is_an_empty_update_and_not_an_error(repo: Path) -> None:
    head = git_head(repo)

    planned = plan(repo, repo=REPO, base=head, head=head)

    assert planned.update.documents == ()
    assert planned.update.deletes == ()
    assert planned.changes == 0


def test_a_full_plan_is_marked_complete_and_names_every_tracked_file(repo: Path) -> None:
    planned = plan_full(repo, repo=REPO)

    assert planned.update.complete is True
    assert planned.update.ref == git_head(repo)
    assert sorted(document.path for document in planned.update.documents) == ["a.py", "b.py"]


def test_a_directory_that_is_not_a_repository_is_a_refusal(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitRefused):
        git_head(plain)


def test_a_bad_revision_is_a_refusal(repo: Path) -> None:
    with pytest.raises(GitRefused):
        git_delta(repo, "does-not-exist", "HEAD")
