"""P24: the consumer the import edges were missing.

`IndexService.importers` had a service endpoint, an adapter and tests — and no one could run
it. This is `forgepilot --dependents`, exercised through `main()` against the reference
service on a real socket, because the thing under test is what an operator sees: a table of
`repo:path@ref`, an empty answer that exits 0, and a refused or unreachable index that exits 2
instead of printing "nobody imports it".
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

pytest.importorskip("fastapi", reason="the reference index needs the `web` extra")
pytest.importorskip("uvicorn", reason="the reference index needs the `web` extra")

from forgepilot.cli.main import main  # noqa: E402
from tests.providers.test_the_index_over_http import (  # noqa: E402
    ALICE,
    API,
    FORGE,
    SERVE,
    WEB,
    _file,
    _free_port,
)

API_SLUG = API.slug


def _corpus(path: Path) -> Path:
    """A corpus with two edges: `api` imports `astropy.modeling`, `web` imports `api.limits`."""
    payload = {
        "files": [
            _file(API, "paging/limits.py", "from astropy.modeling import separable\n"),
            _file(WEB, "paging.py", "from api.limits import clamp\n"),
        ],
        "symbols": [],
        "imports": [
            {"repo": API_SLUG, "path": "paging/limits.py", "target": "astropy.modeling"},
            {"repo": WEB.slug, "path": "paging.py", "target": "api.limits"},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    work = tmp_path_factory.mktemp("dependents-index")
    index = _corpus(work / "index.json")
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


def _run(monkeypatch, service: str, *extra: str, token: str = ALICE) -> int:
    monkeypatch.setenv("FORGEPILOT_INDEX_TOKEN", token)
    argv = [
        "--dependents",
        "astropy.modeling",
        "--index-url",
        service,
        "--index-repo",
        API_SLUG,
        "--index-repo",
        WEB.slug,
        *extra,
    ]
    return main(argv)


def test_it_prints_repository_path_and_revision(monkeypatch, service: str, capsys) -> None:
    assert _run(monkeypatch, service) == 0
    out = capsys.readouterr().out
    assert f"{API_SLUG}:paging/limits.py@main  (imports astropy.modeling)" in out
    assert "1 file(s) import astropy.modeling" in out


def test_a_module_nobody_imports_is_an_empty_answer_not_an_error(
    monkeypatch, service: str, capsys
) -> None:
    """ "Nobody imports it" is a real answer to a real question, so it exits 0 — the exit
    code is reserved for the two answers that must not be mistaken for it."""
    monkeypatch.setenv("FORGEPILOT_INDEX_TOKEN", ALICE)
    code = main(
        [
            "--dependents",
            "nobody.imports",
            "--index-url",
            service,
            "--index-repo",
            API_SLUG,
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "no file in" in out
    assert "nobody.imports" in out


def test_a_repository_outside_the_token_is_refused(monkeypatch, service: str, capsys) -> None:
    monkeypatch.setenv("FORGEPILOT_INDEX_TOKEN", "not-a-token")
    code = main(
        ["--dependents", "astropy.modeling", "--index-url", service, "--index-repo", API_SLUG]
    )
    assert code == 2
    assert "refused" in capsys.readouterr().err.lower()


def test_an_unreachable_index_is_not_an_empty_answer(monkeypatch, capsys) -> None:
    """A closed port must not print "nobody imports it": that would be the worst possible
    wrong answer, because it looks like a fact."""
    port = _free_port()
    monkeypatch.setenv("FORGEPILOT_INDEX_TOKEN", ALICE)
    code = main(
        [
            "--dependents",
            "astropy.modeling",
            "--index-url",
            f"http://127.0.0.1:{port}",
            "--index-repo",
            API_SLUG,
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "could not be answered" in captured.err
    assert "no file in" not in captured.out


def test_a_missing_url_or_repository_is_a_usage_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv("FORGEPILOT_INDEX_TOKEN", ALICE)
    assert main(["--dependents", "x"]) == 2
    assert "--index-url" in capsys.readouterr().err

    assert main(["--dependents", "x", "--index-url", "http://127.0.0.1:1"]) == 2
    assert "--index-repo" in capsys.readouterr().err

    assert (
        main(
            ["--dependents", "x", "--index-url", "http://127.0.0.1:1", "--index-repo", "not-a-slug"]
        )
        == 2
    )
    assert "host/namespace/name" in capsys.readouterr().err


def test_the_two_commands_cannot_be_combined(monkeypatch) -> None:
    """The dispatch refuses two commands at once, because half of each would run."""
    with pytest.raises(SystemExit) as stopped:
        main(["--dependents", "x", "--index-update", API_SLUG])
    assert stopped.value.code == 2
