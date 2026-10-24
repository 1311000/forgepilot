"""An ingestion client against a real service, driven by a real commit.

The unit tests below read a delta out of git. This file closes the loop: index a
repository at one commit, commit a change, send the delta through
`HttpIndexWriter`, and ask the *service* whether the new code is searchable — and
whether the old path has stopped answering. Anything less would be testing the
client against the client.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the reference index needs the `web` extra")
pytest.importorskip("uvicorn", reason="the reference index needs the `web` extra")

from forgepilot.providers.retrieve.remote import HttpIndexService, HttpIndexWriter  # noqa: E402
from forgepilot.runtime.index_update import plan, plan_full  # noqa: E402
from forgepilot.seams.index_service import (  # noqa: E402
    IndexQuery,
    IndexRefused,
    IndexUpdate,
)
from forgepilot.seams.refs import RepoRef  # noqa: E402
from forgepilot.seams.security import ExecutionConstraints  # noqa: E402
from tests.providers.test_the_index_over_http import _free_port  # noqa: E402

FORGE = Path(__file__).resolve().parents[2]
SERVE = FORGE / "tools" / "serve_index.py"
REPO = RepoRef("git.local", "demo", "ingest")
OTHER = RepoRef("git.local", "demo", "other")
TOKEN = "ingest-token"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return done.stdout.strip()


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "ingest@example.test")
    _git(root, "config", "user.name", "ingest")
    (root / "pkg" / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "alpha")
    return root


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    work = tmp_path_factory.mktemp("ingest-service")
    index = work / "index.json"
    index.write_text('{"files": [], "symbols": []}', encoding="utf-8")
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(SERVE),
            "--index",
            str(index),
            "--port",
            str(port),
            "--token",
            f"{TOKEN}=*",
            "--write-token",
            TOKEN,
        ],
        cwd=str(work),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    break
            except OSError:  # pragma: no cover - the wait itself
                time.sleep(0.05)
        else:  # pragma: no cover - only when the service never binds
            pytest.fail("the reference index never started listening")
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged process
            process.kill()


def _constraints(*repos: RepoRef) -> ExecutionConstraints:
    return ExecutionConstraints(
        principal_id="ingest-job",
        tenant_id="demo",
        allowed_repos=frozenset(repos),
    )


async def test_a_commit_becomes_a_delta_the_service_can_search(
    service: str, checkout: Path
) -> None:
    writer = HttpIndexWriter(service, token=TOKEN)
    reader = HttpIndexService(service, token=TOKEN)
    first = plan_full(checkout, repo=REPO)
    await writer.apply(first.update, _constraints(REPO))

    assert await writer.indexed_ref(REPO) == first.update.ref

    (checkout / "pkg" / "beta.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-m", "beta")
    base = first.update.ref
    head = _git(checkout, "rev-parse", "HEAD")
    delta = plan(checkout, repo=REPO, base=base, head=head)
    assert [document.path for document in delta.update.documents] == ["pkg/beta.py"]

    await writer.apply(delta.update, _constraints(REPO))

    assert await writer.indexed_ref(REPO) == head
    hits = await reader.search(
        IndexQuery(pattern="beta", repos=(REPO,), ref=head), _constraints(REPO)
    )
    assert [hit.location.path for hit in hits] == ["pkg/beta.py"]


async def test_a_deleted_file_stops_answering(service: str, checkout: Path) -> None:
    writer = HttpIndexWriter(service, token=TOKEN)
    reader = HttpIndexService(service, token=TOKEN)
    first = plan_full(checkout, repo=REPO)
    await writer.apply(first.update, _constraints(REPO))

    (checkout / "pkg" / "alpha.py").unlink()
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-m", "drop alpha")
    head = _git(checkout, "rev-parse", "HEAD")
    delta = plan(checkout, repo=REPO, base=first.update.ref, head=head)
    assert delta.update.deletes == ("pkg/alpha.py",)
    assert delta.update.documents == ()

    await writer.apply(delta.update, _constraints(REPO))

    hits = await reader.search(
        IndexQuery(pattern="alpha", repos=(REPO,), ref=head), _constraints(REPO)
    )
    assert hits == []


async def test_a_repository_outside_the_credential_is_refused_before_anything_is_sent(
    service: str,
) -> None:
    writer = HttpIndexWriter(service, token=TOKEN)
    update = IndexUpdate(repo=OTHER, ref="main", documents=(), deletes=(), complete=True)
    with pytest.raises(IndexRefused):
        await writer.apply(update, _constraints(REPO))
    assert await writer.indexed_ref(OTHER) is None
