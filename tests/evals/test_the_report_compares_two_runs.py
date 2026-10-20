"""A re-run cannot be read on its own, so the report has to hold two files at once.

`--retry-failed` produces a file holding only the cells that failed. The matrix over
it has no `full` column and therefore nothing to mark; the arm table has one arm. The
question a retry asks is *"did it change"*, and that is a question about two files.

⚠️ **And the reason column is the half that matters.** A cell can hold its rate and
change its behaviour — same `2/3`, a different sentence each time — and that is a
different finding from a stable one. Measured on the real pair: `out-of-scope /
tight-window` went from *"44 tool calls, limit 40"* to *"the agent never mentioned
'settings.py'"* while staying at 2/3.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import report  # noqa: E402


def row(task, arm, rep, *, passed, failures=(), variant=None):
    return {
        "task": task,
        "arm": arm,
        "rep": rep,
        "variant": variant,
        "passed": passed,
        "failures": list(failures),
    }


def test_a_moved_rate_is_flagged(capsys) -> None:
    before = [
        row("t", "arm", n, passed=False, failures=("checks did not pass",)) for n in (1, 2, 3)
    ]
    after = [row("t", "arm", n, passed=True) for n in (1, 2, 3)]

    report._print_comparison(before, after, "old", "new")

    out = capsys.readouterr().out
    assert "0/3" in out and "3/3" in out
    assert "the rate moved" in out


def test_a_cell_that_kept_its_rate_but_changed_its_reason_is_shown(capsys) -> None:
    """⚠️ Same 2/3 on both sides. The rate says "unchanged", and the rate is wrong."""
    before = [
        row("t", "arm", 1, passed=True),
        row("t", "arm", 2, passed=True),
        row("t", "arm", 3, passed=False, failures=("44 tool calls, limit 40",)),
    ]
    after = [
        row("t", "arm", 1, passed=True),
        row("t", "arm", 2, passed=True),
        row("t", "arm", 3, passed=False, failures=("the agent never mentioned 'settings.py'",)),
    ]

    report._print_comparison(before, after, "old", "new")

    out = capsys.readouterr().out
    assert "the rate moved" not in out, "the rate did not move and must not claim to"
    assert "44 tool calls, limit 40" in out
    assert "never mentioned 'settings.py'" in out
    assert "two different failures at the same rate" in out


def test_cells_that_were_not_retried_are_counted_not_tabled(capsys) -> None:
    """A retry holds three cells and the sweep holds thirty. Listing the other
    twenty-seven buries the three this exists to show."""
    before = [row("a", "arm", 1, passed=False, failures=("x",))] + [
        row(name, "arm", 1, passed=True) for name in ("b", "c", "d")
    ]
    after = [row("a", "arm", 1, passed=True)]

    report._print_comparison(before, after, "old", "new")

    out = capsys.readouterr().out
    assert "3 further cell(s)" in out
    for name in ("b", "c", "d"):
        assert f"{name} / arm" not in out, f"{name} was tabled although it was not retried"


def test_two_files_with_nothing_in_common_say_so(capsys) -> None:
    report._print_comparison(
        [row("a", "arm", 1, passed=True)], [row("b", "arm", 1, passed=True)], "old", "new"
    )

    out = capsys.readouterr().out
    assert "nothing to compare" in out


def test_a_variant_is_its_own_cell(capsys) -> None:
    """The same reasoning as `--retry-failed`: `bug-fix` and
    `bug-fix / weakens-the-test` are different questions and must not be compared
    as though one were the other."""
    before = [row("bug-fix", "full", 1, passed=False, failures=("a",), variant="weakens-the-test")]
    after = [row("bug-fix", "full", 1, passed=True, variant="weakens-the-test")]

    report._print_comparison(before, after, "old", "new")

    out = capsys.readouterr().out
    assert "bug-fix / full / weakens-the-test" in out


def test_a_retry_that_changed_nothing_still_prints_the_table(capsys) -> None:
    """**"It did not change" is the answer to the question that prompted the
    retry.** A section that stayed silent would leave the reader unsure the cell had
    been looked at."""
    same = [row("t", "arm", 1, passed=True)]

    report._print_comparison(same, same, "old", "new")

    out = capsys.readouterr().out
    assert "1/1" in out
    assert "the rate moved" not in out
