"""The report has to say **why**, not only **how often**.

`score()` returns reasons, and the reason is the documented point of it — *"how
often, without in what way, is not actionable"*. `run.py` printed them as it went
and they scrolled away; the accumulated report printed a rate and dropped every one
of them. So `report --latest`, which is what a person reads after a run, could not
answer the only question worth asking of an ablation: **which cell moved, and what
happened in it.**

Both defects found in this harness so far were found that way — by reading the
reason, not the number. The second one was not even in `failures`: it was in the
agent's prose, which is why `--agent-text` exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import report  # noqa: E402


def row(task, arm, rep, *, passed, failures=(), state="ready", agent_text=""):
    return {
        "task": task,
        "arm": arm,
        "rep": rep,
        "run": "r1",
        "mode": "live",
        "passed": passed,
        "failures": list(failures),
        "state": state,
        "tool_calls": 5,
        "turns": 1,
        "wall_s": 3.0,
        "agent_text": agent_text,
    }


#: A cell that flipped: three passes under `full`, none under the arm — the shape
#: an ablation exists to produce and the one a rate per arm averages away.
FLIPPED = [
    row("t", "full", 1, passed=True),
    row("t", "full", 2, passed=True),
    row("t", "arm", 1, passed=False, failures=("checks did not pass",), state="failed"),
    row("t", "arm", 2, passed=False, failures=("checks did not pass",), state="failed"),
]


def test_a_failure_reason_reaches_the_report(capsys) -> None:
    report._print_failures(FLIPPED)

    out = capsys.readouterr().out
    assert "checks did not pass" in out
    assert "2x" in out, "the reason was printed without how often it happened"


def test_the_reason_is_attributed_to_the_cell_it_belongs_to(capsys) -> None:
    """A reason with no task and arm beside it is a sentence about the whole run."""
    report._print_failures(FLIPPED)

    out = capsys.readouterr().out
    assert "t / arm" in out
    assert "t / full" not in out, "a cell with no failures was listed as failing"


def test_the_cell_that_dropped_is_marked(capsys) -> None:
    report._print_matrix(FLIPPED)

    out = capsys.readouterr().out
    assert "0/2*" in out, f"the cell that dropped below `full` was not marked:\n{out}"
    assert "3/2" not in out


def test_a_cell_whose_only_failures_are_crashes_is_not_marked_as_an_arm_effect(
    capsys,
) -> None:
    """⚠️ **The 105-row sweep had exactly one of these.**

    `out-of-scope / tight-window` came back 2/3 and the matrix marked it with the
    same `*` as two cells where the agent genuinely behaved differently — so the arm
    table read "tight-window costs 5%", and the third row's reason was
    `error: ConnectError: [Errno 11001] getaddrinfo failed`. One HTTP request in 105
    did not resolve.

    `*` and `!` invite opposite actions: read this cell, versus this cell says
    nothing yet and the sweep is one run short.
    """
    report._print_matrix(
        [
            row("t", "full", 1, passed=True),
            row("t", "full", 2, passed=True),
            row("t", "arm", 1, passed=True),
            row("t", "arm", 2, passed=False, failures=("error: ConnectError",))
            | {"error": "ConnectError: getaddrinfo failed"},
        ]
    )

    out = capsys.readouterr().out
    assert "1/2!" in out, f"a crash was marked as a result about the arm:\n{out}"
    assert "1/2*" not in out
    assert "the arm is not why" in out


def test_a_mixed_cell_is_still_marked_as_an_arm_effect(capsys) -> None:
    """One real failure and one crash is a cell that moved — the crash does not
    excuse the other."""
    report._print_matrix(
        [
            row("t", "full", 1, passed=True),
            row("t", "full", 2, passed=True),
            row("t", "arm", 1, passed=False, failures=("checks did not pass",)),
            row("t", "arm", 2, passed=False, failures=("error: ConnectError",))
            | {"error": "ConnectError"},
        ]
    )

    assert "0/2*" in capsys.readouterr().out


def test_crashes_are_called_out_below_the_failure_reasons(capsys) -> None:
    report._print_failures(
        [row("t", "arm", 1, passed=False, failures=("error: ConnectError",)) | {"error": "x"}]
    )

    out = capsys.readouterr().out
    assert "did not run at all" in out
    assert "`run_one` raised" in out


def test_a_clean_run_does_not_invent_a_failure_section(capsys) -> None:
    """The absence of failures is a fact worth stating, and it is not an empty
    table — an empty table reads as "the section is broken"."""
    report._print_failures([row("t", "full", 1, passed=True)])

    out = capsys.readouterr().out
    assert "No run failed" in out


def test_an_arm_that_changed_nothing_says_so(capsys) -> None:
    """⚠️ **The line that would have caught the first ablation.**

    Three of the five arms were the baseline under another name, and the table they
    produced was indistinguishable from three arms that worked. `(nothing)` is not a
    description of an arm — it is the absence of one.
    """
    report._print_arm_changes(
        [
            row("t", "full", 1, passed=True) | {"arm_changes": {}},
            row("t", "no-repair", 1, passed=True)
            | {"arm_changes": {"retry": ["{'max_attempts': 2}", "{'max_attempts': 0}"]}},
            row("t", "decoy", 1, passed=True) | {"arm_changes": {}},
        ]
    )

    out = capsys.readouterr().out
    assert "(nothing — the baseline configuration)" in out
    assert "retry:" in out and "max_attempts" in out
    assert "decoy" in out and out.count("(nothing") == 2, (
        "a decorative arm must be as visible as the baseline"
    )


def test_rows_written_before_this_was_recorded_do_not_claim_to_be_the_baseline(capsys) -> None:
    """An old results file has no `arm_changes`. Printing `(nothing)` for it would
    be a claim the file does not support."""
    report._print_arm_changes(
        [row("t", "full", 1, passed=True), row("t", "arm", 1, passed=True)]
    )

    out = capsys.readouterr().out
    assert "(not recorded in this file)" in out
    assert "(nothing" not in out


def test_a_reason_with_no_task_and_arm_is_not_lost(capsys) -> None:
    report._print_failures([row("t", "arm", 1, passed=False, failures=())])

    assert "(no reason recorded)" in capsys.readouterr().out


def test_agent_text_is_only_printed_when_asked_for(capsys) -> None:
    """It is model prose and it is long. Opt-in, and it must actually carry the
    text — an empty section would be worse than no section."""
    row_with_prose = row(
        "t", "arm", 1, passed=False, agent_text="I have not modified any source"
    )

    report.main(["--file", "nope"])  # exercises the parser, prints nothing useful
    capsys.readouterr()

    report._print_agent_text([row_with_prose])
    out = capsys.readouterr().out
    assert "I have not modified any source" in out
    assert "t / arm / rep1" in out


@pytest.mark.parametrize("limit", [40, 150])
def test_a_long_reason_is_one_bounded_line(limit: int) -> None:
    """A traceback in the middle of a table helps nobody."""
    shortened = report._shorten("word " * 500, limit)

    assert len(shortened) <= limit
    assert "\n" not in shortened
