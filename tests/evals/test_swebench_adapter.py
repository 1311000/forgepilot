"""The SWE-bench adapter refuses what it could not measure honestly.

A benchmark number is the easiest kind of claim to state and the hardest to
defend: the questions are somebody else's, the checkouts are pinned revisions this
box cannot download, and a runner that says nothing is indistinguishable from a
runner that passed everything. These tests pin the three refusals — a malformed
instance, an unpinned checkout, and a result nobody could read — because each one,
if it slipped through, would become a score.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

FORGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import swebench as S  # noqa: E402

SHA = "a" * 40


def record(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "instance_id": "astropy__astropy-12907",
        "repo": "astropy/astropy",
        "base_commit": SHA,
        "problem_statement": "the windowed mean loses a window",
        "FAIL_TO_PASS": json.dumps(["tests/test_stats.py::test_window"]),
        "PASS_TO_PASS": json.dumps(["tests/test_stats.py::test_other"]),
        "test_patch": "",
        "patch": "",
    }
    data.update(overrides)
    return data


def instances_file(tmp_path: Path, *records: dict[str, object]) -> Path:
    path = tmp_path / "instances.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
    return path


def _one(**overrides: object) -> S.SwebenchInstance:
    data = record(**overrides)
    return S.SwebenchInstance(
        instance_id=str(data["instance_id"]),
        repo=str(data["repo"]),
        base_commit=str(data["base_commit"]),
        problem_statement=str(data["problem_statement"]),
        fail_to_pass=("tests/test_stats.py::test_window",),
        pass_to_pass=("tests/test_stats.py::test_other",),
    )


# ---------------------------------------------------------------------------
# Parsing: what is refused before anything is spent
# ---------------------------------------------------------------------------
def test_the_published_dataset_shape_is_read(tmp_path: Path) -> None:
    """`FAIL_TO_PASS` is a *string holding a list* in the real file."""
    loaded = S.load_instances(instances_file(tmp_path, record()))
    assert len(loaded) == 1
    assert loaded[0].fail_to_pass == ("tests/test_stats.py::test_window",)
    assert loaded[0].pass_to_pass == ("tests/test_stats.py::test_other",)
    assert loaded[0].slug == "astropy__astropy"


def test_a_record_with_nothing_to_prove_is_refused(tmp_path: Path) -> None:
    with pytest.raises(S.MalformedInstance, match="FAIL_TO_PASS"):
        S.load_instances(instances_file(tmp_path, record(FAIL_TO_PASS=[])))


def test_an_unpinned_revision_is_refused(tmp_path: Path) -> None:
    """ "HEAD" would measure whatever the checkout happens to be."""
    with pytest.raises(S.MalformedInstance, match="base_commit"):
        S.load_instances(instances_file(tmp_path, record(base_commit="HEAD")))


def test_a_duplicate_instance_is_refused(tmp_path: Path) -> None:
    with pytest.raises(S.MalformedInstance, match="duplicate"):
        S.load_instances(instances_file(tmp_path, record(), record()))


def test_a_repository_without_an_owner_is_refused() -> None:
    with pytest.raises(S.MalformedInstance, match="owner/name"):
        S.slug_of("astropy")


def test_a_blank_line_is_not_an_instance(tmp_path: Path) -> None:
    path = instances_file(tmp_path, record())
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert len(S.load_instances(path)) == 1


def test_a_missing_dataset_names_what_is_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="SWE-bench_Lite"):
        S.load_instances(tmp_path / "nope.jsonl")


# ---------------------------------------------------------------------------
# Grading: `unknown` is not `resolved`
# ---------------------------------------------------------------------------
def test_a_still_failing_required_test_is_unresolved() -> None:
    one = _one()
    assert S.verdict(one, ["tests/test_stats.py::test_window"]) == "unresolved"


def test_a_broken_regression_test_is_unresolved_too() -> None:
    one = _one()
    assert S.verdict(one, ["tests/test_stats.py::test_other"]) == "unresolved"


def test_a_clean_run_is_resolved() -> None:
    assert S.verdict(_one(), []) == "resolved"


def test_a_runner_that_said_nothing_is_unknown_not_resolved() -> None:
    """An empty id list is what a green pytest run reports *and* what an
    unparseable runner reports; only the caller knows which, so it must say."""
    assert S.verdict(_one(), [], runner_told_us=False) == "unknown"


def test_a_run_that_never_happened_is_not_run() -> None:
    assert S.verdict(_one(), [], ran=False) == "not_run"


# ---------------------------------------------------------------------------
# The spike's cheap precondition
# ---------------------------------------------------------------------------
def git(path: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - argv, no shell
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def checkout(tmp_path: Path, *, name: str = "astropy__astropy-12907") -> tuple[Path, str]:
    path = tmp_path / "repos" / name
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "user.name", "t")
    (path / "stats.py").write_text("MAX = 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    return path, git(path, "rev-parse", "HEAD")


def test_a_checkout_at_the_pinned_revision_is_ready(tmp_path: Path) -> None:
    path, head = checkout(tmp_path)
    rows = S.spike([_one(base_commit=head)], tmp_path / "repos")
    assert rows[0].ok, rows[0].reason
    assert rows[0].head == head
    assert rows[0].path == path


def test_a_checkout_at_another_revision_is_refused(tmp_path: Path) -> None:
    """The failure the spike exists to catch before spending a model call."""
    checkout(tmp_path)
    rows = S.spike([_one(base_commit="b" * 40)], tmp_path / "repos")
    assert not rows[0].ok
    assert "not base_commit" in rows[0].reason


def test_a_missing_checkout_is_refused(tmp_path: Path) -> None:
    rows = S.spike([_one()], tmp_path / "repos")
    assert not rows[0].ok
    assert "no checkout" in rows[0].reason


def test_a_directory_that_is_not_a_checkout_is_refused(tmp_path: Path) -> None:
    (tmp_path / "repos" / "astropy__astropy-12907").mkdir(parents=True)
    rows = S.spike([_one()], tmp_path / "repos")
    assert not rows[0].ok
    assert "not a git checkout" in rows[0].reason


def test_two_instances_of_one_repository_are_two_checkouts(tmp_path: Path) -> None:
    """The published dataset's first three rows are one repo at three revisions.

    An `owner__name` layout holds one of them, so the other two were refused for a
    reason that was the layout's fault rather than the checkout's — measured against
    the real dataset on 2026-10-20, after the adapter had passed its unit tests.
    """
    first, head = checkout(tmp_path, name="astropy__astropy-12907")
    second, other = checkout(tmp_path, name="astropy__astropy-14182")
    rows = S.spike(
        [
            _one(instance_id="astropy__astropy-12907", base_commit=head),
            _one(instance_id="astropy__astropy-14182", base_commit=other),
        ],
        tmp_path / "repos",
    )
    assert [row.ok for row in rows] == [True, True]
    assert {rows[0].path, rows[1].path} == {first, second}
