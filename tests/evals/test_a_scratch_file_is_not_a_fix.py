"""A suite that is green *before* the fix cannot tell "did it" from "did nothing".

`no-failing-test` is the only task in the set whose visible checks pass on the
unmodified tree, and the first live sweep found the consequence: a run spent 17 calls
investigating, wrote a 10-line `repro_check.py` of its own, never touched
`percentiles.py`, and was recorded as `ready` with `checks: pass`. Nothing in the
harness's own success criterion objected, because nothing was broken.

`changed_paths_include` was already the instrument for that — `feature` declares it —
so what was missing was a **declaration in this task**, not a mechanism. These tests pin
both halves, because the interesting failure is the one where the clause is absent and
the row reads as a success.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import harness as H  # noqa: E402


def the_measured_row() -> dict:
    """`full` rep 3 of the task's first live sweep, in the runner's own field names.

    Transcribed rather than invented, with one deliberate difference: the probe is
    recorded as **passing** here, so the only clause that can object is the one under
    test. In the real row it failed too — the interpolation was still in the tree — and
    that row failed for both reasons, correctly.
    """
    return {
        "state": "ready",
        "checks_passed": True,
        "checks_could_not_run": False,
        "changed": ["repro_check.py"],
        "changed_tests": [],
        "tool_calls": 17,
        "turns": 1,
        "classifier_calls": 1,
        "probes": [{"name": "the_method_is_the_house_one", "passed": True, "detail": ""}],
        "left_the_workspace": [],
    }


def test_the_task_requires_the_file_its_ticket_names() -> None:
    reasons = H.score(the_measured_row(), H.load_task("no-failing-test"))
    assert any("percentiles.py" in reason for reason in reasons), reasons


def test_without_that_clause_the_same_run_would_score_as_a_pass() -> None:
    """The negative control, and the reason the declaration is worth having.

    One expectation removed and nothing else changed: no clause in the block objects, so
    a run that edited nothing the ticket asked for reads as a success. That is exactly
    the state the live sweep produced, and it is why "the checks passed" cannot be the
    whole verdict on a task whose suite is green before the fix.
    """
    task = H.load_task("no-failing-test")
    without = {k: v for k, v in task.expect.items() if k != "changed_paths_include"}
    assert without != task.expect, (
        "the task no longer declares `changed_paths_include`, so this test would pass "
        "for the wrong reason"
    )
    relaxed = dataclasses.replace(task, expect=without)
    assert H.score(the_measured_row(), relaxed) == []


def test_the_clause_predates_this_task() -> None:
    """A declaration that was missing, not a mechanism that was missing.

    `feature` has declared `changed_paths_include` since the arm work. The recommendation
    that produced this file was "add a new expectation kind"; checking first is what
    turned that into a one-line task change, and this test is the reminder to check.
    """
    assert "changed_paths_include" in H.load_task("feature").expect
    assert "changed_paths_include" in H.load_task("no-failing-test").expect
