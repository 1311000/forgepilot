"""P15: the write side, exercised over a real socket.

The read side is driven through loopback in `test_the_index_over_http.py`. This is the other
half: an ingestion job says "here is a new revision", and the next search must see it, the
removed file must stop being served, and a token that may only read must not be able to write.

The service is its own process on its own port with its own corpus, so these mutations cannot
reach any other test module.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

pytest.importorskip("fastapi", reason="the reference index needs the `web` extra")
pytest.importorskip("uvicorn", reason="the reference index needs the `web` extra")

from forgepilot.seams.index_service import lag  # noqa: E402
from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef  # noqa: E402
from tests.providers.test_the_index_over_http import (  # noqa: E402
    ALICE,
    API,
    BOB,
    FORGE,
    SERVE,
    WEB,
    _client,
    _constraints,
    _free_port,
    _write_index,
)

pytestmark = pytest.mark.asyncio

NEW_FILE = "def shared(requested: int, limit: int) -> int:\n    return min(requested, limit)\n"


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A separate process, with two writers: one unrestricted, one scoped to `API`."""
    work = tmp_path_factory.mktemp("writable-index")
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
            "--write-token",
            ALICE,
            "--write-token",
            BOB,
        ],
        cwd=str(FORGE),
        stdout=subprocess.DEVNULL,
        # Not PIPE: a traceback is larger than a pipe buffer, and a server blocked writing
        # its own error to an unread pipe stops answering — which reads as a timeout.
        stderr=log,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0

    def _log() -> str:
        log.flush()
        return (work / "service.log").read_text(encoding="utf-8", errors="replace")[-2000:]

    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"the index service exited with {process.returncode}: {_log()}")
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


async def _seed(base: str) -> None:
    """Put `API` back to its fixture files at `main`, so each test starts from one state."""
    await _apply(
        base,
        ALICE,
        repo=API.slug,
        ref="main",
        documents=[
            {"path": "limits.py", "text": "MAX_PER_PAGE = 250\n"},
            {"path": "corpus-only/remote_only.py", "text": "REMOTE = 1\n"},
        ],
        complete=True,
    )


async def test_a_new_document_is_searchable_immediately(service: str) -> None:
    await _seed(service)
    client = _client(service)
    result = await _apply(
        service,
        ALICE,
        repo=API.slug,
        ref="main",
        documents=[{"path": "paging/_shared_guard.py", "text": NEW_FILE}],
    )
    assert result["repo"] == API.slug

    found = await _search(service, "shared", repo=API)
    assert [hit.location.path for hit in found] == ["paging/_shared_guard.py"]
    assert found[0].location.ref == "main"

    # The write produced symbols too: an updated file that cannot be found by name would be
    # half-indexed, which is worse than not indexed because it looks complete.
    hits = await client.symbols(_symbol_query("shared", API), _constraints(API))
    assert [(hit.name, hit.kind) for hit in hits] == [("shared", "function")]


async def test_a_deleted_file_stops_being_served(service: str) -> None:
    await _seed(service)
    assert await _search(service, "MAX_PER_PAGE", repo=API), "the fixture must start with it"
    await _apply(service, ALICE, repo=API.slug, ref="main", deletes=["limits.py"])
    assert await _search(service, "MAX_PER_PAGE", repo=API) == []
    with pytest.raises(FileNotFoundError):
        await _client(service).open(
            CodeRef(repo=API, path="limits.py", ref="main"), LineSpan(1, 10), _constraints(API)
        )


async def test_a_complete_snapshot_removes_what_it_does_not_name(service: str) -> None:
    await _seed(service)
    await _apply(
        service,
        ALICE,
        repo=API.slug,
        ref="main",
        documents=[{"path": "only.py", "text": "VALUE = 1\n"}],
        complete=True,
    )
    assert await _search(service, "MAX_PER_PAGE", repo=API) == []
    found = await _search(service, "VALUE", repo=API)
    assert [hit.location.path for hit in found] == ["only.py"]
    # A delta on one repository must not touch another one.
    assert await _search(service, "WINDOW", repo=WEB)


async def test_a_scoped_writer_may_write_its_own_repository_and_no_other(service: str) -> None:
    """`BOB` may read `API` and write `API`; `WEB` is outside both."""
    await _seed(service)
    inside = await _apply(
        service, BOB, repo=API.slug, ref="main", documents=[{"path": "bob.py", "text": "BOB = 1\n"}]
    )
    assert inside["files"] >= 3
    with pytest.raises(PermissionError) as denied:
        await _apply(
            service, BOB, repo=WEB.slug, ref="main", documents=[{"path": "x.py", "text": "X = 1\n"}]
        )
    assert "may not write" in str(denied.value)


async def test_an_unknown_token_cannot_write_anything(service: str) -> None:
    with pytest.raises(PermissionError) as denied:
        await _apply(service, "not-a-token", repo=API.slug, ref="main", documents=[])
    assert "not known" in str(denied.value)


async def test_the_status_names_the_ref_it_holds_per_repository(service: str) -> None:
    """Without this a caller cannot tell a fresh answer from a stale one."""
    await _apply(service, ALICE, repo=API.slug, ref="release/2.0", documents=[])
    status = await _client(service).status(_constraints(API))
    assert status.refs_by_repo[API.slug] == "release/2.0"
    assert lag(status, API, "main").behind


# -- helpers -------------------------------------------------------------------------------


async def _search(service: str, pattern: str, *, repo: RepoRef):
    from forgepilot.seams.index_service import IndexQuery

    return await _client(service).search(
        IndexQuery(pattern=pattern, repos=(repo,), ref="main"), _constraints(repo)
    )


def _symbol_query(name: str, repo: RepoRef):
    from forgepilot.seams.index_service import IndexSymbolQuery

    return IndexSymbolQuery(name=name, repos=(repo,), ref="main")


async def _apply(
    service: str,
    token: str,
    *,
    repo: str,
    ref: str,
    documents=(),
    deletes=(),
    complete: bool = False,
) -> dict:
    """`/index` is a service endpoint, not part of the retriever client: call it directly."""
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as http:
        response = await http.post(
            f"{service}/index",
            json={
                "repo": repo,
                "ref": ref,
                "documents": list(documents),
                "deletes": list(deletes),
                "complete": complete,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code != 200:
        raise PermissionError(response.text)
    return response.json()
