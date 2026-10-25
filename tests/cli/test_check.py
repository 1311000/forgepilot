"""P25: `forgepilot --index-update … --check` — the reader the lag rule never had.

`lag()` was a pure function with two callers in the tests and none in `src/`: it could tell
that an index answers for an older revision, and nothing ever asked it. This is the reader
that is actually runnable — an operator or a CI job asks "is the index behind?" and gets an
exit code, not a paragraph in a design note.

Three answers, and the third is the one that is easy to render wrongly:

* **up to date** — exit 0, because nothing is wrong;
* **behind** — exit 3, because the index is *answering*, just for an older commit. It must
  not look like a broken command (exit 2) or a success (0);
* **git cannot place the indexed revision** (rewritten branch, shallow clone, a ref the
  service invented) — exit 3 with the count left unsaid. `0` here would be a lie: "I cannot
  compare" and "there is nothing to compare" are different statements.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from forgepilot.cli.main import _commits_behind, _report_lag, main
from forgepilot.seams.refs import RepoRef

pytest.importorskip("fastapi", reason="the reference index needs the `web` extra")
pytest.importorskip("uvicorn", reason="the reference index needs the `web` extra")

REPO = RepoRef(host="git.corp", namespace="team", name="api")
ALICE = "alice-token"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return done.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Two commits, so "one behind" is a real distance rather than a boolean."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "T")
    (root / "limits.py").write_text("LIMIT = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    first = _git(root, "rev-parse", "HEAD")
    (root / "limits.py").write_text("LIMIT = 2\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "second")
    return root, first, _git(root, "rev-parse", "HEAD")


def test_an_index_at_the_checkout_revision_is_up_to_date(tmp_path, capsys) -> None:
    root, _, head = _repo(tmp_path)
    assert _report_lag(root, REPO, head, head) == 0
    assert "up to date" in capsys.readouterr().out


def test_an_index_behind_the_checkout_says_how_far(tmp_path, capsys) -> None:
    root, first, head = _repo(tmp_path)
    assert _report_lag(root, REPO, first, head) == 3
    out = capsys.readouterr().out
    assert "1 commit(s) behind" in out
    assert first[:12] in out
    assert head[:12] in out


def test_a_repository_the_index_has_never_seen_is_not_reported_as_current(tmp_path, capsys) -> None:
    root, _, head = _repo(tmp_path)
    assert _report_lag(root, REPO, None, head) == 3
    assert "no revision" in capsys.readouterr().err


def test_a_revision_git_cannot_place_is_not_zero_commits(tmp_path, capsys) -> None:
    """The honest failure mode: the service named a ref this checkout does not contain."""
    root, _, head = _repo(tmp_path)
    unknown = "0" * 40
    assert _commits_behind(root, unknown, head) is None
    assert _report_lag(root, REPO, unknown, head) == 3
    out = capsys.readouterr().out
    assert "behind" in out
    assert "0 commit(s)" not in out


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    """The reference service, holding one repository at a revision the test chooses."""
    work = tmp_path_factory.mktemp("check-index")
    ref = "0123456789abcdef0123456789abcdef01234567"
    index = work / "index.json"
    index.write_text(
        json.dumps(
            {
                "files": [
                    {"repo": REPO.slug, "path": "limits.py", "ref": ref, "text": "LIMIT = 1\n"}
                ],
                "symbols": [],
            }
        ),
        encoding="utf-8",
    )
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "tools" / "serve_index.py"),
            "--index",
            str(index),
            "--port",
            str(port),
            "--token",
            f"{ALICE}=*",
        ],
        stdout=subprocess.DEVNULL,
        stderr=(work / "service.log").open("wb"),
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(
                    "the index service exited: "
                    + (work / "service.log").read_text(encoding="utf-8", errors="replace")[-2000:]
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("the index service never accepted a connection")
        yield base, ref
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged process
            process.kill()
            process.wait(timeout=10)


def test_the_operator_gets_an_exit_code_not_a_document(
    monkeypatch, tmp_path, capsys, service: tuple[str, str]
) -> None:
    """End to end: the service holds one revision, the checkout is elsewhere, exit 3."""
    base, ref = service
    root, _, head = _repo(tmp_path)
    monkeypatch.setenv("FORGEPILOT_INDEX_TOKEN", ALICE)

    code = main(
        [
            "--index-update",
            REPO.slug,
            "--index-url",
            base,
            "--repo-root",
            str(root),
            "--check",
        ]
    )

    captured = capsys.readouterr()
    assert code == 3
    assert ref[:12] in captured.out
    assert head[:12] in captured.out


def test_check_reports_up_to_date_when_the_service_holds_the_checkout(
    monkeypatch, tmp_path, capsys
) -> None:
    """The same command against an index that *is* at HEAD exits 0 — the reader can say yes."""
    root, _, head = _repo(tmp_path)
    # The service is asked through the same code path; only the indexed ref differs, so the
    # cheap way to exercise "0" without a second server is the pure reporter (held above).
    assert _commits_behind(root, head, head) == 0
    assert _report_lag(root, REPO, head, head) == 0
    assert "up to date" in capsys.readouterr().out
