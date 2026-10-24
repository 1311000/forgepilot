"""A sharded index, over a real socket: one shard answers late, and the answer is partial.

The in-process tests in `test_the_index_fans_out.py` pin the decisions. This file pins the
part only a socket can show: the same query, over a real port, with one shard past its
deadline — and what the real client does with the result. The rule under test is the seam's
first rule: *a question that could not be fully asked is not a question that was answered
with nothing*.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi", reason="the reference index needs the `web` extra")
pytest.importorskip("uvicorn", reason="the reference index needs the `web` extra")

from forgepilot.providers.retrieve.remote import HttpIndexService  # noqa: E402
from forgepilot.seams.index_service import IndexQuery, IndexUnavailable  # noqa: E402
from forgepilot.seams.refs import RepoRef  # noqa: E402
from forgepilot.seams.security import ExecutionConstraints  # noqa: E402

FORGE = Path(__file__).resolve().parents[2]
SERVE = FORGE / "tools" / "serve_index.py"

MONO = RepoRef("git.corp", "team", "mono")
TOKEN = "alice-token"
DELAY_S = 0.6
DEADLINE_MS = 150


def _write_index(path: Path) -> Path:
    def entry(where: str, text: str) -> dict[str, Any]:
        return {"repo": MONO.slug, "path": where, "ref": "main", "text": text}

    payload = {
        "files": [
            entry("web/app.py", "def app():\n    return 'web'\n"),
            entry("api/handlers.py", "def handle():\n    return 'api'\n"),
            entry("README.md", "# mono\n"),
        ],
        "symbols": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def sharded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    work = tmp_path_factory.mktemp("sharded-index")
    index = _write_index(work / "index.json")
    port = _free_port()
    log = (work / "service.log").open("wb")
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
            "--shard-by",
            "top-dir",
            "--shard-deadline-ms",
            str(DEADLINE_MS),
            "--shard-delay",
            f"{MONO.slug}#web={DELAY_S}",
        ],
        cwd=str(FORGE),
        stdout=subprocess.DEVNULL,
        stderr=log,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0

    def _stderr() -> str:
        log.flush()
        return (work / "service.log").read_text(encoding="utf-8", errors="replace")[-2000:]

    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"the index service exited with {process.returncode}: {_stderr()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("the index service never accepted a connection")
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged process
            process.kill()
            process.wait(timeout=10)


def _constraints() -> ExecutionConstraints:
    return ExecutionConstraints(
        principal_id="alice", tenant_id="demo", allowed_repos=frozenset({MONO})
    )


def _service(base: str) -> HttpIndexService:
    return HttpIndexService(base, token=TOKEN, timeout_s=10.0)


def test_status_reports_the_layout_and_every_shard(sharded: str) -> None:
    body = httpx.get(
        f"{sharded}/status", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10.0
    ).json()
    assert body["shardBy"] == "top-dir"
    by_name = {shard["shard"]: shard for shard in body["shards"]}
    assert set(by_name) == {MONO.slug, f"{MONO.slug}#api", f"{MONO.slug}#web"}
    assert by_name[f"{MONO.slug}#web"]["prefix"] == "web/"
    assert by_name[MONO.slug]["prefix"] == ""
    assert by_name[MONO.slug]["files"] == 1


async def test_a_query_that_needs_the_slow_shard_is_refused_not_answered_partially(
    sharded: str,
) -> None:
    service = _service(sharded)
    with pytest.raises(IndexUnavailable) as caught:
        await service.search(
            IndexQuery(pattern="app", repos=(MONO,), ref="main", globs=("web/**",)),
            _constraints(),
        )
    said = str(caught.value)
    assert f"{MONO.slug}#web" in said
    assert "not an empty result" in said


async def test_a_query_that_skips_the_slow_shard_still_answers(sharded: str) -> None:
    service = _service(sharded)
    hits = await service.search(
        IndexQuery(pattern="handle", repos=(MONO,), ref="main", globs=("api/**",)),
        _constraints(),
    )
    assert [hit.location.path for hit in hits] == ["api/handlers.py"]


def test_a_caller_who_can_wait_gets_the_slow_shard_too(sharded: str) -> None:
    body = httpx.post(
        f"{sharded}/search",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "pattern": "app",
            "repos": [MONO.slug],
            "ref": "main",
            "deadlineMs": int((DELAY_S + 1.0) * 1000),
        },
        timeout=20.0,
    ).json()
    assert body["shards"]["failed"] == []
    assert f"{MONO.slug}#web" in body["shards"]["answered"]
    assert [hit["path"] for hit in body["hits"]] == ["web/app.py"]
