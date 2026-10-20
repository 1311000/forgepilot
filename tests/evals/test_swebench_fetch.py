"""The fetch step writes a file, then proves the adapter can read it.

No network is touched: the opener is replaced, because a test that needs HuggingFace
is a test that fails for a reason unrelated to the code. What is asserted is the
contract that matters — the fields that survive, the paging, and that a payload the
adapter would refuse is reported rather than left on disk as if it were usable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

FORGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE / "tools"))
sys.path.insert(0, str(FORGE))

import swebench_fetch as F  # noqa: E402


def row(instance_id: str, **overrides: object) -> dict[str, Any]:
    data: dict[str, Any] = {
        "instance_id": instance_id,
        "repo": "astropy/astropy",
        "base_commit": "a" * 40,
        "problem_statement": "the windowed mean loses a window",
        "FAIL_TO_PASS": json.dumps(["tests/test_stats.py::test_window"]),
        "PASS_TO_PASS": json.dumps([]),
        "test_patch": "",
        "patch": "",
        "created_at": "2023-01-01",
        "version": "5.0",
    }
    data.update(overrides)
    return data


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeOpener:
    """Serves `num_rows_total` rows, `length` at a time, and records the URLs."""

    def __init__(self, rows: list[dict[str, Any]], *, fail: Exception | None = None) -> None:
        self.rows = rows
        self.fail = fail
        self.urls: list[str] = []

    def open(self, request: Any, timeout: float = 0.0) -> FakeResponse:
        url = request.full_url
        self.urls.append(url)
        if self.fail is not None:
            raise self.fail
        query = dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))
        offset = int(query["offset"])
        length = int(query["length"])
        chunk = self.rows[offset : offset + length]
        return FakeResponse(
            {"rows": [{"row": item} for item in chunk], "num_rows_total": len(self.rows)}
        )


def write(monkeypatch: pytest.MonkeyPatch, opener: FakeOpener, out: Path, **kwargs: Any) -> Any:
    monkeypatch.setattr(F, "_opener", lambda proxy: opener)
    return F.fetch(out=out, api="http://fake.invalid", **kwargs)


def test_only_the_fields_the_adapter_reads_are_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "instances.jsonl"
    ids = write(monkeypatch, FakeOpener([row("astropy__astropy-12907")]), out, limit=1)
    assert ids == ("astropy__astropy-12907",)
    written = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert set(written) == set(F.KEPT)
    assert "created_at" not in written


def test_it_pages_until_the_limit_is_reached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opener = FakeOpener([row(f"astropy__astropy-{n}") for n in range(12907, 12912)])
    ids = write(monkeypatch, opener, tmp_path / "i.jsonl", limit=3, page=2)
    assert len(ids) == 3
    assert len(opener.urls) == 2, opener.urls


def test_a_payload_the_adapter_refuses_is_named_not_left_usable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "instances.jsonl"
    broken = row("astropy__astropy-12907", FAIL_TO_PASS=json.dumps([]))
    with pytest.raises(SystemExit) as caught:
        write(monkeypatch, FakeOpener([broken]), out, limit=1)
    message = str(caught.value)
    assert "refuses it" in message
    assert "FAIL_TO_PASS is empty" in message


def test_an_unreachable_dataset_names_the_proxy_as_the_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import urllib.error

    failure = urllib.error.URLError("connection refused")
    with pytest.raises(SystemExit) as caught:
        write(monkeypatch, FakeOpener([], fail=failure), tmp_path / "i.jsonl", limit=1)
    assert "--proxy" in str(caught.value)


def test_a_non_positive_limit_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        F.fetch(out=tmp_path / "i.jsonl", limit=0)


def test_an_empty_dataset_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        write(monkeypatch, FakeOpener([]), tmp_path / "i.jsonl", limit=1)
    assert "returned no rows" in str(caught.value)
