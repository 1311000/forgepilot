"""The index builder: a corpus a deployment can actually produce.

`providers/retrieve/index.py` was tested only against a nine-line fixture somebody typed,
which is enough to pin its rules and not enough to make it a deployment: without a builder
the provider can only ever be exercised against a corpus that already exists in a test. So
the builder's job is to turn a real checkout into that JSON, and its two obligations are
both about honesty rather than volume:

- **The revision it records is the one it read.** `git rev-parse HEAD`, plus `+dirty` when
  the tree is not clean — the same convention the ripgrep provider uses. An index labelled
  with a branch name while its content came from a dirty tree would repeat that lie in
  every hit and every slice.
- **What it skipped is reported.** `.git/`, oversized files and non-UTF-8 files are left
  out by name and by count; a corpus that quietly lost half its files answers "there is
  nothing there" to a question it never really asked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FORGE = Path(__file__).resolve().parents[2]
BUILDER = FORGE / "tools" / "build_code_index.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

REPO = "git.corp/team/svc-a"

APP = '''"""A fixture module."""


class Retry:
    def backoff(self, attempt):
        return attempt


def retry_login(client):
    return client.login()
'''


def _repo(path: Path) -> Path:
    (path / "src").mkdir(parents=True)
    (path / "src" / "app.py").write_text(APP, encoding="utf-8")
    (path / "README.md").write_text("# svc-a\n", encoding="utf-8")
    for argv in (
        ["init", "-b", "main"],
        ["add", "-A"],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@test",
            "commit",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(["git", *argv], cwd=str(path), check=True, capture_output=True)
    return path


def _build(root: Path, out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--repo",
            REPO,
            "--root",
            str(root),
            "--out",
            str(out),
            *extra,
        ],
        cwd=str(FORGE),
        check=False,
        capture_output=True,
        text=True,
    )


def test_it_turns_a_checkout_into_an_index(tmp_path: Path) -> None:
    root = _repo(tmp_path / "svc-a")
    out = tmp_path / "index.json"

    completed = _build(root, out)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [entry["path"] for entry in payload["files"]] == ["README.md", "src/app.py"]
    assert {entry["repo"] for entry in payload["files"]} == {REPO}
    # `paths` use `/` even on Windows: an index whose paths matched nothing the rest of
    # the system reports would answer "not there" about files it is holding.
    assert all("\\" not in entry["path"] for entry in payload["files"])


def test_it_records_the_commit_it_read(tmp_path: Path) -> None:
    root = _repo(tmp_path / "svc-a")
    out = tmp_path / "index.json"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout.strip()

    _build(root, out)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert {entry["ref"] for entry in payload["files"]} == {head}


def test_a_dirty_tree_is_recorded_as_dirty(tmp_path: Path) -> None:
    root = _repo(tmp_path / "svc-a")
    (root / "src" / "app.py").write_text(APP + "\n# uncommitted\n", encoding="utf-8")
    out = tmp_path / "index.json"

    completed = _build(root, out)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert all(entry["ref"].endswith("+dirty") for entry in payload["files"])


def test_an_explicit_ref_is_recorded(tmp_path: Path) -> None:
    root = _repo(tmp_path / "svc-a")
    out = tmp_path / "index.json"

    _build(root, out, "--ref", "release/1.0")

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert {entry["ref"] for entry in payload["files"]} == {"release/1.0"}


def test_python_symbols_carry_a_span_and_a_signature(tmp_path: Path) -> None:
    root = _repo(tmp_path / "svc-a")
    out = tmp_path / "index.json"

    _build(root, out)

    payload = json.loads(out.read_text(encoding="utf-8"))
    found = {(entry["name"], entry["kind"]) for entry in payload["symbols"]}
    assert ("Retry", "class") in found
    assert ("backoff", "method") in found
    assert ("retry_login", "function") in found
    for entry in payload["symbols"]:
        assert entry["start"] <= entry["end"]
        assert entry["signature"], "a symbol with no signature cannot be shown to a reader"
        assert entry["path"] == "src/app.py"


def test_a_file_that_is_not_utf8_is_skipped_and_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path / "svc-a")
    (root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    out = tmp_path / "index.json"

    completed = _build(root, out)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert all(entry["path"] != "blob.bin" for entry in payload["files"])
    assert "not UTF-8" in completed.stderr


def test_an_oversized_file_is_skipped_and_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path / "svc-a")
    (root / "big.txt").write_text("x" * 500, encoding="utf-8")
    out = tmp_path / "index.json"

    completed = _build(root, out, "--max-bytes", "100")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert all(entry["path"] != "big.txt" for entry in payload["files"])
    assert "over 100 bytes" in completed.stderr


def test_ignored_files_are_left_out(tmp_path: Path) -> None:
    """`.gitignore` is the corpus's first filter — a walk of this repository indexed
    `.venv/` and produced 8 875 files / 58 MB of installed packages."""
    root = _repo(tmp_path / "svc-a")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (root / "debug.log").write_text("noise\n", encoding="utf-8")
    out = tmp_path / "index.json"

    completed = _build(root, out)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert all(entry["path"] != "debug.log" for entry in payload["files"])
    assert any(entry["path"] == "README.md" for entry in payload["files"])


def test_a_plain_directory_works_when_a_ref_is_given(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    out = tmp_path / "index.json"

    completed = _build(root, out, "--ref", "main")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [entry["path"] for entry in payload["files"]] == ["a.txt"]
    assert {entry["ref"] for entry in payload["files"]} == {"main"}
    assert "not a git checkout" in completed.stderr


def test_a_slug_that_is_not_three_parts_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "svc-a")
    out = tmp_path / "index.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--repo",
            "svc-a",
            "--root",
            str(root),
            "--out",
            str(out),
        ],
        cwd=str(FORGE),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "--repo" in completed.stderr
    assert not out.exists()


def test_a_directory_that_is_not_a_repository_needs_an_explicit_ref(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("hello\n", encoding="utf-8")

    completed = _build(root, tmp_path / "index.json")

    assert completed.returncode != 0
    assert "--ref" in completed.stderr
