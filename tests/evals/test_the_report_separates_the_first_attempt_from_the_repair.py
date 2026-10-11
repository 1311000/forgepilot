"""One `pass` rate hides the mechanism; `pass@1` and `pass@2` do not.

The eval's whole subject is **whether each mechanism earns its place**, and the
repair loop is the most expensive mechanism in the set: it spends a model call per
retry. A row that recorded only the final verdict made the loop unfalsifiable —
a run rescued on the second attempt and a run that worked first time produced the
same `pass`, so `no-repair` could not be shown to matter even when it did.

Aider's leaderboard is the precedent, and its polyglot numbers are why this is
worth copying: **gpt-5 (high) 52.0% → 88.0%** over 225 exercises. That ~36-point
gap *is* the error-feedback loop quantified, and it is invisible in a single rate.
Aider also reports it the way this file does — `pass@1` and `pass@2` cumulative,
so `pass@2 >= pass@1` always.

⚠️ **And an old results file must not be read as a zero.** Rows written before the
attempts were recorded have no `check_verdicts`; `-` is the honest answer, and the
report says which file it is looking at rather than printing a rate nobody
measured. Same rule as `arm_changes` printing `(not recorded in this file)`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import report  # noqa: E402


def row(task, arm, rep, *, passed, verdicts=None, **extra):
    """A results row. ``verdicts=None`` means the key is absent — an old file."""
    base = {
        "task": task,
        "arm": arm,
        "rep": rep,
        "run": "r1",
        "mode": "live",
        "passed": passed,
        "failures": [] if passed else ["checks did not pass"],
        "state": "ready" if passed else "failed",
        "tool_calls": 5,
        "turns": 2,
        "wall_s": 4.0,
    }
    if verdicts is not None:
        base["check_verdicts"] = verdicts
    base.update(extra)
    return base


#: The shape the split exists for: three runs, one of which the loop rescued.
RESCUED = [
    row("t", "full", 1, passed=True, verdicts=[True]),
    row("t", "full", 2, passed=True, verdicts=[False, True]),
    row("t", "full", 3, passed=False, verdicts=[False, False]),
]


def test_a_run_the_repair_loop_rescued_is_not_counted_as_a_first_attempt_pass() -> None:
    """The defect this whole change is about: both runs `pass`, one of them cost a
    model call, and the single rate cannot tell you which."""
    assert report._pass_at(RESCUED[0], 1) is True
    assert report._pass_at(RESCUED[1], 1) is False

    assert report._pass_at(RESCUED[0], 2) is True
    assert report._pass_at(RESCUED[1], 2) is True


def test_the_two_rates_are_cumulative_so_the_gap_is_the_loops_work() -> None:
    """`pass@2 >= pass@1` always, and the difference is the conversion count.

    If these were two *disjoint* buckets — "solved at exactly attempt 1" and
    "solved at exactly attempt 2" — they could not be compared with each other or
    with anyone else's leaderboard.
    """
    ones = [report._pass_at(r, 1) for r in RESCUED]
    twos = [report._pass_at(r, 2) for r in RESCUED]

    assert all(two >= one for one, two in zip(ones, twos))
    assert sum(ones) == 1 and sum(twos) == 2, "one rescue should be one conversion"


def test_the_columns_appear_and_are_not_the_pass_column(capsys) -> None:
    report._print_grouped(RESCUED)

    out = capsys.readouterr().out
    assert "pass@1" in out and "pass@2" in out
    # 2 of 3 rows passed overall, 1 first time, 2 within two.
    assert "1/3" in out and "2/3" in out


def test_an_old_file_reads_as_unknown_rather_than_as_zero(capsys) -> None:
    """⚠️ **A missing number is not a zero.**

    Every results file on disk before this change has no `check_verdicts`. Counting
    those rows as "failed the first attempt" would have invented a mechanism rate
    out of an absence — the exact shape of defect this project keeps recording.
    """
    old = [row("t", "full", 1, passed=True), row("t", "full", 2, passed=True)]

    report._print_grouped(old)
    out = capsys.readouterr().out

    assert "0/2" not in out, f"an old file was read as two first-attempt failures:\n{out}"
    assert "not recorded" in out or "before the attempts" in out


def test_a_row_that_never_got_to_a_check_is_a_failure_not_an_unknown() -> None:
    """An empty list is a *recorded* fact: the plan never became ready, so no
    attempt was made. That is not the same as "nobody wrote it down"."""
    never_ran = row("t", "full", 1, passed=False, verdicts=[])

    assert report._pass_at(never_ran, 1) is False
    assert report._pass_at(never_ran, 2) is False


def test_the_gap_section_names_the_mechanism_it_is_about(capsys) -> None:
    report._print_attempt_gap(RESCUED)

    out = capsys.readouterr().out
    assert "repair loop" in out
    assert "first attempt : 1/3" in out
    assert "within two attempts  : 2/3" in out
    assert "converted 1" in out, "the conversion count is the number the loop has to justify"


def test_a_loop_that_converted_nothing_says_so_rather_than_printing_a_zero(capsys) -> None:
    """On tasks where nothing failed first, the loop has nothing to show — and an
    ablation of it will show nothing. That is a result, and printing it beats
    letting a reader assume the loop is doing work."""
    report._print_attempt_gap([row("t", "full", 1, passed=True, verdicts=[True])])

    out = capsys.readouterr().out
    assert "converted nothing" in out


def test_the_gap_section_is_absent_when_no_row_recorded_attempts(capsys) -> None:
    """No section rather than an empty one: a heading with nothing under it reads
    as a broken printer."""
    report._print_attempt_gap([row("t", "full", 1, passed=True)])

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("attempts", [1, 2])
def test_one_attempt_recorded_still_answers_both_questions(attempts: int) -> None:
    """`no-repair` produces single-attempt rows, and `pass@2` must still be
    answerable for them — otherwise the arm whose whole point is the loop would be
    the one arm the columns cannot describe."""
    single = row("t", "no-repair", 1, passed=True, verdicts=[True])

    assert report._pass_at(single, attempts) is True
