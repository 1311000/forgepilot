"""A row that never finishes must not be able to hang the sweep.

⚠️ **Measured 2026-10-19, live.** A cell of `stale-corpus/full` ran a command
whose child was `python -`, which waits on a stdin nobody closes. `run_one`'s
"never raises" contract covers exceptions — it does not cover *not returning* —
so the sweep sat in that cell for twenty minutes with one row written and no
verdict, and the only thing that ended it was a human killing the process.

A sweep that can hang is worse than one that loses a row: the missing row looks
exactly like a cell nobody got round to, while the truth is "the harness was
still waiting when the operator gave up". The watchdog turns that into a row
that says `timed-out`, which is a result.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import run as run_module  # noqa: E402


def rows_of(results: Path) -> list[dict]:
    path = max(results.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_a_row_that_never_returns_is_written_down_and_the_sweep_continues(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    async def hangs_forever(*args, **kwargs):
        await asyncio.sleep(3_600)

    monkeypatch.setattr(run_module, "run_one", hangs_forever)
    monkeypatch.setattr(run_module, "RESULTS", tmp_path)

    code = run_module.main(
        [
            "--scripted",
            "--task",
            "bug-fix",
            "--arm",
            "full",
            "--reps",
            "1",
            "--row-timeout",
            "0.05",
        ]
    )

    assert code == 0
    (row,) = rows_of(tmp_path)
    assert row["state"] == "timed-out"
    assert row["passed"] is False
    assert row["wall_s"] == 0.05
    assert row["tool_calls"] == 0
    # The row has to say *why*, or a timed-out cell is indistinguishable from a
    # cell that failed for a reason the task cares about.
    assert "did not finish within" in row["failures"][0]
    assert "stdin" in row["failures"][0]
    # And the operator has to see it on the console, not only in the file.
    assert "timed out" in capsys.readouterr().err
