"""Every task is priced, and the ceiling can actually refuse a row.

`evals/ceilings.py` is a table of tool-call bounds derived from 782 measured live rows, and
`evals/run.py` fails a row that goes over its bound. Two things make that a mechanism
rather than decoration, and both are checked here:

- **The table covers the tasks that exist.** A new task with no entry would run unbounded
  while the sweep still looked measured — the same shape as a field nobody reads.
- **The comparison can fail.** `exceeded` is driven here with a count one over a real
  ceiling and one under it, so a version that always returned `None` (or always returned a
  reason) would be caught by the test rather than by a green matrix.

The module is imported the way the other eval tests import the harness: `evals/` is a
directory of scripts, so the test puts it on `sys.path` first.
"""

from __future__ import annotations

import sys
from pathlib import Path

FORGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import ceilings  # noqa: E402
import harness as H  # noqa: E402


def test_every_task_has_a_ceiling() -> None:
    tasks = H.available_tasks()
    assert tasks, "no eval tasks were found, so this test proves nothing"
    missing = [task for task in tasks if ceilings.ceiling(task) is None]
    assert not missing, (
        f"these tasks have no cost ceiling: {missing}. Add them to `evals/ceilings.py` — a "
        f"task that is not priced runs unbounded while the sweep still looks measured."
    )
    stale = [task for task in ceilings.CALL_CEILINGS if task not in tasks]
    assert not stale, f"`evals/ceilings.py` prices tasks that do not exist: {stale}"


def test_the_ceiling_can_refuse_a_row() -> None:
    task = "bug-fix"
    bound = ceilings.ceiling(task)
    assert bound is not None
    assert ceilings.exceeded(task, bound) is None
    reason = ceilings.exceeded(task, bound + 1)
    assert reason is not None
    assert task in reason and str(bound) in reason


def test_a_task_that_must_not_act_has_a_ceiling_of_zero() -> None:
    # `incomplete`'s correct answer is a question; one tool call is already the failure it
    # measures, so a ceiling above zero would let the task's own defect pass as cheap.
    assert ceilings.ceiling("incomplete") == 0
    assert ceilings.exceeded("incomplete", 0) is None
    assert ceilings.exceeded("incomplete", 1) is not None


def test_a_row_without_a_count_is_not_judged() -> None:
    # "no number" is a different defect from "too many"; claiming it passed a ceiling would
    # be the field-that-looks-enforced shape this repository keeps finding.
    assert ceilings.exceeded("bug-fix", None) is None
    assert ceilings.exceeded("bug-fix", "not a number") is None


def test_an_unpriced_task_is_not_silently_allowed() -> None:
    assert ceilings.ceiling("a-task-that-does-not-exist") is None
    assert ceilings.exceeded("a-task-that-does-not-exist", 10_000) is None
