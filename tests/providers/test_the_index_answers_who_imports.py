"""P22: "who imports this" answered by the service, over a real socket.

The edge is a static import statement, not a call graph, and these tests are about the
consequences of that: a file's edges are *replaced* when it is re-sent, they disappear when it
is deleted, and the question is matched by whole components so `astropy.modeling` is not
answered by `astropy.modeling_extra`.
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

from forgepilot.seams.index_service import IndexImportQuery  # noqa: E402
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

IMPORTER = "from astropy.modeling import separable\nimport numpy as np\n"


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    work = tmp_path_factory.mktemp("importable-index")
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
        stderr=log,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log.flush()
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
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged process
            process.kill()
            process.wait(timeout=10)


async def _index(service: str, *, documents=(), imports=(), deletes=(), complete: bool = False):
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as http:
        response = await http.post(
            f"{service}/index",
            json={
                "repo": API.slug,
                "ref": "main",
                "documents": list(documents),
                "imports": list(imports),
                "deletes": list(deletes),
                "complete": complete,
            },
            headers={"Authorization": f"Bearer {ALICE}"},
        )
    assert response.status_code == 200, response.text
    return response.json()


async def _importers(service: str, module: str, *, token: str = ALICE, repo=API):
    return await _client(service, token=token).importers(
        IndexImportQuery(module=module, repos=(repo,), ref="main"), _constraints(repo)
    )


async def test_a_file_that_imports_a_module_is_named(service: str) -> None:
    await _index(
        service,
        documents=[{"path": "paging/limits.py", "text": IMPORTER}],
        imports=[
            {"path": "paging/limits.py", "target": "astropy.modeling"},
            {"path": "paging/limits.py", "target": "numpy"},
        ],
        complete=True,
    )

    found = await _importers(service, "numpy")
    assert [(hit.location.path, hit.target, hit.location.ref) for hit in found] == [
        ("paging/limits.py", "numpy", "main")
    ]

    # Whole components: asking for the parent names the child's importer ...
    assert [hit.target for hit in await _importers(service, "astropy")] == ["astropy.modeling"]
    # ... and asking for a longer name does not match a shorter one.
    assert await _importers(service, "astropy.modeling.separable") == []


async def test_a_near_miss_is_not_a_match(service: str) -> None:
    """`astropy.modeling_extra` must not answer a question about `astropy.modeling`."""
    await _index(
        service,
        documents=[
            {"path": "other.py", "text": "import astropy.modeling_extra\n"},
            {"path": "exact.py", "text": "import astropy.modeling\n"},
        ],
        imports=[
            {"path": "other.py", "target": "astropy.modeling_extra"},
            {"path": "exact.py", "target": "astropy.modeling"},
        ],
        complete=True,
    )
    found = await _importers(service, "astropy.modeling")
    assert [(hit.location.path, hit.target) for hit in found] == [("exact.py", "astropy.modeling")]


async def test_an_edge_is_replaced_when_the_file_is_resent(service: str) -> None:
    await _index(
        service,
        documents=[{"path": "moving.py", "text": "import numpy\n"}],
        imports=[{"path": "moving.py", "target": "numpy"}],
        complete=True,
    )
    assert [hit.location.path for hit in await _importers(service, "numpy")] != []

    # The file no longer imports it: keeping the old edge would answer with a file that has
    # stopped importing the module, which is a wrong answer wearing a right shape.
    await _index(
        service,
        documents=[{"path": "moving.py", "text": "import json\n"}],
        imports=[{"path": "moving.py", "target": "json"}],
    )
    assert [hit.location.path for hit in await _importers(service, "numpy")] == []
    assert [hit.location.path for hit in await _importers(service, "json")] == ["moving.py"]


async def test_a_deleted_file_takes_its_edges_with_it(service: str) -> None:
    await _index(
        service,
        documents=[{"path": "gone.py", "text": "import hashlib\n"}],
        imports=[{"path": "gone.py", "target": "hashlib"}],
        complete=True,
    )
    assert await _importers(service, "hashlib") != []
    await _index(service, deletes=["gone.py"])
    assert await _importers(service, "hashlib") == []


async def test_a_repository_outside_the_credential_is_never_asked(service: str) -> None:
    """`BOB` may read `API`; a question naming `WEB` is filtered before it is sent."""
    await _index(
        service,
        documents=[{"path": "own.py", "text": "import numpy\n"}],
        imports=[{"path": "own.py", "target": "numpy"}],
        complete=True,
    )
    found = await _importers(service, "numpy", token=BOB, repo=WEB)
    assert found == []
