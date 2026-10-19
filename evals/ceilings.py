"""What a task is allowed to cost, measured once and written down.

**Why calls and not seconds or dollars.** A wall-clock ceiling measures this machine, and
the same task on a slower box would fail for a reason that says nothing about the agent;
a dollar ceiling needs a price table that goes stale the moment a model is swapped. A
`tool_calls` count is deterministic under `--scripted`, survives a hardware change, and is
the number the budget guard already enforces per turn (`Budget.max_tool_calls`), so a
ceiling here and a ceiling in a deployment mean the same thing.

**Where the numbers come from.** 782 live rows recorded between 2026-10-11 and 2026-10-19
(every arm, every task, including the 160-cell and 96-cell sweeps). Each ceiling is
`1.5 x p95`, rounded up to a multiple of five, with one deliberate exception:
`incomplete` must use **zero** calls, because that task's correct answer is a question and
any tool call is already the failure it measures.

**What a ceiling is and is not.** It is a regression alarm: an agent that suddenly needs 40
calls where it needed 12 has changed behaviour, and the change is worth a failing row
rather than a line in a report nobody reads. It is *not* a proof that the work was cheap —
a cheaper run that fails the task is worse, and `passed` still says so. When a ceiling and
a good run disagree, the ceiling is what moves, in a commit that says why: the one thing it
must not become is a number quietly raised until the red stops.
"""

from __future__ import annotations

from typing import Any

__all__ = ["CALL_CEILINGS", "ceiling", "exceeded"]

#: Tool calls a task may spend. Keys are task directory names, so a task without an entry
#: is a task nobody has priced yet — `tests/evals/test_a_task_costs_what_its_ceiling_says.py`
#: fails on that, rather than letting a new task run unbounded while looking measured.
CALL_CEILINGS: dict[str, int] = {
    "bug-fix": 25,
    "feature": 25,
    "find-the-bug": 40,
    "find-the-rule": 40,
    "incomplete": 0,
    "no-failing-test": 30,
    "not-in-the-checkout": 50,
    "out-of-scope": 35,
    "stale-corpus": 30,
    "two-attempts": 20,
    "two-repos": 55,
}


def ceiling(task: str) -> int | None:
    """The bound for this task, or `None` when nobody has priced it."""
    return CALL_CEILINGS.get(task)


def exceeded(task: str, tool_calls: Any) -> str | None:
    """Why this call count is over the ceiling, or `None` when it is not.

    A returned sentence rather than a boolean, so a report can say *what* happened — the
    same reason `harness.score` returns reasons. A row with no numeric `tool_calls` is not
    judged: that is a different defect (a row that never recorded its cost), and claiming
    it passed a ceiling would be the "field that looks enforced" shape this project keeps
    finding.
    """
    bound = ceiling(task)
    if bound is None:
        return None
    try:
        calls = int(tool_calls)
    except (TypeError, ValueError):
        return None
    if calls <= bound:
        return None
    return (
        f"the agent spent {calls} tool calls; {task!r} has a ceiling of {bound}. "
        f"The ceilings live in `evals/ceilings.py` because a run that suddenly costs three "
        f"times what it did is a change worth a failing row."
    )
