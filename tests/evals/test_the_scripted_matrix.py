"""The offline matrix's own check has to be able to fail.

`evals/matrix.py` asserts a table measured on 2026-10-18 and runs in CI's scheduled job.
A checker that only ever prints "every cell matches" would be this repository's oldest
defect shape wearing a new hat — a claim nothing reads — so these tests drive it with
rows that are wrong in each of the ways a real run can be wrong, and one row set that is
exactly right.

The module is loaded by path rather than imported by name: `evals/matrix.py` is a script
in a directory that is not a package, and giving it a unique module name here keeps a
future `matrix` import in the tests from picking this one up.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

FORGE = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def matrix() -> Any:
    spec = importlib.util.spec_from_file_location("eval_matrix", FORGE / "evals" / "matrix.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rows_for(matrix: Any, *, passed: dict[tuple[str, str], bool] | None = None) -> list[dict]:
    """One row per expected cell, passing exactly where the table says it should."""
    out = []
    for arm, failures in matrix.EXPECTED_FAILURES.items():
        for task in matrix.H.available_tasks():
            ok = task not in failures
            if passed is not None:
                ok = passed.get((task, arm), ok)
            out.append({"task": task, "arm": arm, "passed": ok})
    return out


def test_a_row_scored_with_the_answers_within_reach_is_not_a_cell(matrix) -> None:
    """The M23 sweep produced three of these: green rows whose agent had read
    `evals/tasks/not-in-the-checkout/solution/` through the shell. The row stays in the
    file (it happened) and it stops being a measurement — otherwise the weekly job would
    report a table full of passes that mean nothing."""
    rows = rows_for(matrix)
    for row in rows:
        row["scratch_inside_repo"] = True

    problems = matrix.check(rows)

    assert any("within reach" in line for line in problems)
    assert all("no row at all" not in line for line in problems)


def test_the_measured_table_passes_its_own_check(matrix: Any) -> None:
    assert matrix.check(rows_for(matrix)) == []


def test_a_row_that_flipped_is_named_with_both_verdicts(matrix: Any) -> None:
    rows = rows_for(matrix, passed={("two-attempts", "no-repair"): True})
    problems = matrix.check(rows)
    assert len(problems) == 1
    assert "two-attempts/no-repair" in problems[0]
    assert "expected fail, got pass" in problems[0]


def test_a_missing_cell_is_not_a_silent_pass(matrix: Any) -> None:
    rows = [r for r in rows_for(matrix) if (r["task"], r["arm"]) != ("bug-fix", "full")]
    assert "bug-fix/full: no row at all" in matrix.check(rows)


def test_the_same_cell_twice_is_a_problem(matrix: Any) -> None:
    rows = rows_for(matrix)
    problems = matrix.check([*rows, dict(rows[0])])
    assert any("the same cell appears twice" in problem for problem in problems)


def test_an_arm_that_claims_to_change_nothing_and_did(matrix: Any) -> None:
    rows = rows_for(matrix)
    for row in rows:
        if row["arm"] == "full":
            row["arm_changes"] = {"retriever": ["grep", "none"]}
    assert any("switched" in problem and "not nothing" in problem for problem in matrix.check(rows))


def test_a_cell_the_table_does_not_name_is_reported(matrix: Any) -> None:
    rows = [*rows_for(matrix), {"task": "bug-fix", "arm": "an-arm-that-was-renamed", "passed": True}]
    assert any("a cell the table does not name" in problem for problem in matrix.check(rows))
