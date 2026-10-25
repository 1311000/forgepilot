"""P30: the load driver's own arithmetic, and one real run against a real socket.

Two levels, because they fail differently:

* `percentile` is where a load report lies most cheaply — an interpolated p95 prints a latency
  no request took, and an empty sample set prints `0 ms` as if the server were instant. The
  unit tests pin nearest-rank and the empty case.
* the smoke test starts *one replica* on a free port and drives it. It keeps the numbers small
  (two workers, two steps) because it is asserting that the table exists and the exit code is
  honest, not that the machine is fast — the measured numbers live in
  `docs/notes/deployment.md`, where they can be dated.
"""

from __future__ import annotations

import json

import pytest

from tools.load_single_replica import main, percentile

pytest.importorskip("fastapi", reason="the load driver needs a server to drive")
pytest.importorskip("uvicorn", reason="the load driver needs a server to drive")


def test_an_empty_sample_set_is_zero_and_not_a_lie() -> None:
    """No samples means no latency; a table that prints one would be inventing it."""
    assert percentile([], 0.95) == 0.0


def test_one_sample_is_that_sample_at_every_percentile() -> None:
    assert percentile([0.25], 0.5) == 0.25
    assert percentile([0.25], 0.99) == 0.25


def test_percentiles_are_values_some_request_actually_took() -> None:
    """Nearest rank: with ten samples p95 is the tenth, not an interpolation between two."""
    samples = [float(value) for value in range(1, 11)]
    assert percentile(samples, 0.5) == 5.0
    assert percentile(samples, 0.95) == 10.0
    assert percentile(samples, 1.0) == 10.0
    assert percentile(samples, 0.0) == 1.0


def test_percentiles_do_not_care_about_input_order() -> None:
    assert percentile([3.0, 1.0, 2.0], 0.5) == 2.0


def test_a_percentile_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


def test_a_run_against_one_replica_prints_a_table_and_exits_zero(tmp_path) -> None:
    """End to end: start a replica, drive it, stop it. Small numbers, honest table."""
    code = main(
        [
            "--start",
            "--state",
            str(tmp_path / "state"),
            "--workspaces",
            str(tmp_path / "work"),
            "--concurrency",
            "2",
            "--requests",
            "2",
            "--kinds",
            "turns",
        ]
    )
    assert code == 0


def test_the_json_form_carries_the_same_rows(capsys, tmp_path) -> None:
    code = main(
        [
            "--start",
            "--state",
            str(tmp_path / "state2"),
            "--workspaces",
            str(tmp_path / "work2"),
            "--concurrency",
            "1",
            "--requests",
            "2",
            "--kinds",
            "reads",
            "--json",
        ]
    )
    assert code == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    rows = {row["kind"]: row for row in lines if "kind" in row}
    assert rows["read"]["n"] == 2
    assert rows["read"]["failures"] == {}
    assert any("wall_s" in row for row in lines)
