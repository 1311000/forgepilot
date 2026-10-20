"""`HttpIndexService` against a service that is actually listening.

Every other test of the remote provider uses `httpx.MockTransport`, which proves the client
agrees with *itself*: the wire shape never crosses a socket, so "a deployment's service can
meet this interface" was a sentence about a dictionary. This test starts
`tools/serve_index.py` as its own process, on a real port, and drives the real client through
it — translation, serialisation, status mapping, refs and refusals together.

The refusals are the part worth having: a 401 that arrives as an empty result would let a
permission boundary read as "the code is not there", which is exactly the confusion the
`IndexService` seam was written to forbid.
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

import pytest

pytest.importorskip("fastapi", reason="the reference index needs the `web` extra")
pytest.importorskip("uvicorn", reason="the reference index needs the `web` extra")

from forgepilot.providers.retrieve.remote import HttpIndexService  # noqa: E402
from forgepilot.seams.index_service import (  # noqa: E402
    IndexQuery,
    IndexRefused,
    IndexSymbolQuery,
    IndexUnavailable,
)
from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef  # noqa: E402
from forgepilot.seams.retriever import WORKING_TREE  # noqa: E402
from forgepilot.seams.security import ExecutionConstraints, Operation  # noqa: E402

FORGE = Path(__file__).resolve().parents[2]
SERVE = FORGE / "tools" / "serve_index.py"

API = RepoRef("git.corp", "team", "api")
WEB = RepoRef("git.corp", "team", "web")
ALICE = "alice-token"
BOB = "bob-token"


def _file(repo: RepoRef, path: str, text: str, ref: str = "main") -> dict[str, Any]:
    return {"repo": repo.slug, "path": path, "ref": ref, "text": text}


def _write_index(path: Path) -> Path:
    payload = {
        "files": [
            _file(
                API,
                "limits.py",
                "\n".join(
                    [
                        "MAX_PER_PAGE = 250",
                        "",
                        "def clamp(requested: int, limit: int) -> int:",
                        "    return min(requested, limit)",
                        "",
                        "def page(requested: int) -> int:",
                        "    return clamp(requested, MAX_PER_PAGE)",
                    ]
                ),
            ),
            _file(WEB, "paging.py", "from api.limits import clamp\n\nWINDOW = clamp(10, 5)\n"),
            # A path that exists nowhere on this machine: the proof that an answer came
            # from the service rather than from a checkout is that there is no checkout
            # to have read it from.
            _file(API, "corpus-only/remote_only.py", "REMOTE = 1\n"),
        ],
        "symbols": [
            {
                "repo": API.slug,
                "path": "limits.py",
                "ref": "main",
                "name": "clamp",
                "kind": "function",
                "start": 3,
                "end": 4,
                "signature": "def clamp(requested: int, limit: int) -> int:",
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A separate process, listening, with two tokens of different reach."""
    work = tmp_path_factory.mktemp("index-service")
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
            f"{ALICE}=*",
            "--token",
            f"{BOB}={API.slug}",
            "--updated-at",
            "2026-10-20T00:00:00Z",
        ],
        cwd=str(FORGE),
        stdout=subprocess.DEVNULL,
        # Not PIPE: a traceback is larger than a pipe buffer, and a server blocked writing
        # its own error to an unread pipe stops answering — which reads as a timeout, not
        # as the 500 it actually answered.
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


def _constraints(*repos: RepoRef) -> ExecutionConstraints:
    return ExecutionConstraints(
        principal_id="alice",
        tenant_id="acme",
        allowed_repos=frozenset(repos),
        permitted_operations=frozenset({Operation.READ_CODE}),
    )


def _client(base: str, token: str = ALICE) -> HttpIndexService:
    return HttpIndexService(base, token=token)


async def test_status_reports_what_the_corpus_holds(served: str) -> None:
    status = await _client(served).status(_constraints(API, WEB))
    assert status.repositories == 2
    assert status.files == 3
    assert status.refs == ("main",)
    assert status.updated_at == "2026-10-20T00:00:00Z"


async def test_a_search_crosses_a_socket_and_names_its_revision(served: str) -> None:
    hits = await _client(served).search(
        IndexQuery(pattern="def clamp", repos=(API,), ref=WORKING_TREE),
        _constraints(API),
    )
    assert [hit.location.path for hit in hits] == ["limits.py"]
    # `<worktree>` is a request, not an answer: the service says which revision it served.
    assert hits[0].location.ref == "main"
    assert hits[0].line == 3


async def test_a_slice_is_the_text_the_index_holds(served: str) -> None:
    served_slice = await _client(served).open(
        CodeRef(repo=API, path="limits.py", ref="main"), LineSpan(3, 4), _constraints(API)
    )
    assert "def clamp" in served_slice.text
    assert served_slice.location.ref == "main"
    assert served_slice.indexed_at == "2026-10-20T00:00:00Z"
    assert served_slice.span == LineSpan(3, 4)


async def test_a_slice_could_not_have_come_from_this_disk(served: str) -> None:
    """The service holds a file no checkout here has, and the client reads it back."""
    assert not (FORGE / "corpus-only").exists(), "this proof needs a path that is not on disk"
    served_slice = await _client(served).open(
        CodeRef(repo=API, path="corpus-only/remote_only.py", ref="main"),
        LineSpan(1, 1),
        _constraints(API),
    )
    assert served_slice.text.strip() == "REMOTE = 1"


async def test_symbols_carry_the_span_the_index_built(served: str) -> None:
    hits = await _client(served).symbols(
        IndexSymbolQuery(name="clamp", repos=(API,), ref="main"), _constraints(API)
    )
    assert len(hits) == 1
    assert hits[0].span == LineSpan(3, 4)
    assert str(hits[0].kind) == "function"


async def test_a_repository_the_token_may_not_read_is_refused_not_empty(served: str) -> None:
    """The distinction the seam exists for: 403 is an answer, `[]` is a lie."""
    with pytest.raises(IndexRefused):
        await _client(served, BOB).search(
            IndexQuery(pattern="clamp", repos=(WEB,), ref="main"), _constraints(WEB)
        )


async def test_the_same_token_still_serves_what_it_may_read(served: str) -> None:
    hits = await _client(served, BOB).search(
        IndexQuery(pattern="clamp", repos=(API,), ref="main"), _constraints(API)
    )
    assert hits, "a token that may read this repository must get its code back"
    assert {hit.location.path for hit in hits} == {"limits.py"}


async def test_an_unknown_token_is_a_refusal_not_an_outage(served: str) -> None:
    with pytest.raises(IndexRefused):
        await _client(served, "not-a-token").status(_constraints(API))


async def test_a_missing_slice_is_a_file_not_found(served: str) -> None:
    with pytest.raises(FileNotFoundError):
        await _client(served).open(
            CodeRef(repo=API, path="nowhere.py", ref="main"), LineSpan(1, 5), _constraints(API)
        )


async def test_a_dead_service_is_unavailable_rather_than_empty() -> None:
    """The other half of "an error is not an empty result": nothing listening at all."""
    client = HttpIndexService(f"http://127.0.0.1:{_free_port()}", token=ALICE, timeout_s=5.0)
    with pytest.raises(IndexUnavailable):
        await client.status(_constraints(API))
