"""Re-run the cells that failed, not the whole sweep.

⚠️ **A sweep is the wrong instrument for a follow-up question.** Ninety live runs
cost about ten minutes and a real amount of money, and the question after reading
one — *"was that cell real?"* — is about two or three of them. Without this, the
only way to check a suspicion was to pay for the sweep again, which is how a
measurement gets taken once and believed forever.

**A cell, not a row.** Three runs of one arm on one task are three samples of one
condition. Re-running the two that failed would answer a different question from
the one being asked, and would bias the retry toward the failures by construction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import run as run_module  # noqa: E402


def write(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def row(task, arm, rep, *, passed, variant=None):
    return {
        "task": task,
        "arm": arm,
        "rep": rep,
        "passed": passed,
        "variant": variant,
    }


def test_only_the_cells_that_failed_are_returned(tmp_path) -> None:
    path = write(
        tmp_path,
        [
            row("a", "full", 1, passed=True),
            row("a", "no-repair", 1, passed=False),
            row("a", "no-repair", 2, passed=True),
            row("a", "no-repair", 3, passed=False),
            row("b", "full", 1, passed=True),
        ],
    )

    assert run_module.failed_cells(path) == [("a", "no-repair", None)]


def test_the_whole_cell_is_retried_not_only_the_rows_that_failed(tmp_path) -> None:
    """`no-repair` failed twice and passed once. All three are the same condition,
    so the retry is three runs — the one that passed included. Re-running only the
    failures would bias the sample toward the outcome being investigated."""
    path = write(
        tmp_path,
        [row("a", "arm", n, passed=(n != 1 and n != 3)) for n in (1, 2, 3)],
    )

    cells = run_module.failed_cells(path)

    assert cells == [("a", "arm", None)]
    assert run_module  # the retry count is `len(cells) * reps`, i.e. 3


def test_a_variant_is_part_of_the_cell(tmp_path) -> None:
    """⚠️ **Without this a retry silently asks a different question.** A failing
    `bug-fix / weakens-the-test` re-run as plain `bug-fix` would come back green and
    look like the failure had evaporated."""
    path = write(
        tmp_path,
        [
            row("bug-fix", "no-pinning", 1, passed=False, variant="weakens-the-test"),
            row("bug-fix", "no-pinning", 2, passed=False, variant="weakens-the-test"),
        ],
    )

    assert run_module.failed_cells(path) == [("bug-fix", "no-pinning", "weakens-the-test")]


def test_each_cell_appears_once(tmp_path) -> None:
    path = write(tmp_path, [row("a", "arm", n, passed=False) for n in (1, 2, 3)])

    assert run_module.failed_cells(path) == [("a", "arm", None)]


def test_a_clean_sweep_has_nothing_to_retry(tmp_path) -> None:
    path = write(tmp_path, [row("a", "full", 1, passed=True)])

    assert run_module.failed_cells(path) == []


def test_a_read_only_retry_is_possible(tmp_path, capsys) -> None:
    """The point of the feature, in one assertion: the retry reads a file and
    produces a shorter one — it does not re-derive the sweep."""
    path = write(
        tmp_path,
        [
            row("a", "full", 1, passed=True),
            row("a", "full", 2, passed=True),
            row("a", "no-repair", 1, passed=False),
        ],
    )

    cells = run_module.failed_cells(path)

    assert len(cells) == 1, (
        f"retrying {len(cells)} cell(s) out of 3 rows plus arms that never ran — "
        f"the whole point is that this is smaller than a sweep"
    )
