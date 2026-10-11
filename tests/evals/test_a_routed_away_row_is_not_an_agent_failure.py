"""A row the classifier declined is not an agent failure.

`harness.boot` swaps in the fixed classifier only under `--scripted`, so **a live run's
ticket is classified by the real model** — the `understanding:` block in a task file is a
replay fixture. When the model declines, the run ends `awaiting_info` with no turns and no
tool calls, and the row fails however good the agent would have been. Measured on the first
sweep where it happened: 1 row in 138.

The three conditions are the signature, and each is load-bearing:

- **the state**, because that is what a decline produces;
- **no work done**, because a run that took turns and then ended `awaiting_info` is a
  different, real failure;
- **the row failing**, because `incomplete` *expects* `awaiting_info` — testing only the
  state would hide that task's results, which is the same mistake in the other direction.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import report  # noqa: E402


def a_row(
    *,
    arm: str,
    rep: int,
    passed: bool,
    state: str = "ready",
    turns: int = 1,
    tool_calls: int = 5,
    task: str = "two-attempts",
) -> dict:
    return {
        "task": task,
        "arm": arm,
        "rep": rep,
        "passed": passed,
        "state": state,
        "turns": turns,
        "tool_calls": tool_calls,
        "wall_s": 10.0,
        "mode": "live",
        "run": "a-test",
    }


def test_the_signature_needs_all_three_conditions() -> None:
    declined = a_row(arm="full", rep=3, passed=False, state="awaiting_info", turns=0, tool_calls=0)
    assert report._routed_away(declined) is True

    # `incomplete` expects this state and passes on it — not a decline.
    expected = a_row(arm="full", rep=1, passed=True, state="awaiting_info", turns=0, tool_calls=0)
    assert report._routed_away(expected) is False

    # A run that did work and still failed is a real failure, whatever the state.
    work = a_row(arm="full", rep=1, passed=False, state="failed", turns=2, tool_calls=9)
    assert report._routed_away(work) is False

    # A decline after work is the interesting middle case, and it is a failure.
    late = a_row(arm="full", rep=1, passed=False, state="awaiting_info", turns=2, tool_calls=7)
    assert report._routed_away(late) is False


def test_a_declined_row_does_not_lower_the_arms_rate(capsys) -> None:
    report._print_arms(
        [
            a_row(arm="full", rep=1, passed=True),
            a_row(arm="full", rep=2, passed=True),
            a_row(arm="full", rep=3, passed=False, state="awaiting_info", turns=0, tool_calls=0),
            a_row(arm="baseline", rep=1, passed=True),
        ]
    )
    out = capsys.readouterr().out

    assert "100%" in out, "the rate is over the rows that ran, not over every row in the file"
    assert "routed" in out, "the excluded count has to be visible"
    assert "two-attempts/full/rep3" in out, "and the row has to be named, not just counted"


def test_the_matrix_marks_the_cell_instead_of_shrinking_the_fraction(capsys) -> None:
    report._print_matrix(
        [
            a_row(arm="full", rep=1, passed=True),
            a_row(arm="full", rep=2, passed=True),
            a_row(arm="full", rep=3, passed=False, state="awaiting_info", turns=0, tool_calls=0),
            a_row(arm="baseline", rep=1, passed=True),
            a_row(arm="baseline", rep=2, passed=True),
        ]
    )
    out = capsys.readouterr().out

    assert "2/2r" in out, "the routed row is marked, and the fraction is over the rows that ran"
    assert "r = a row was routed away" in out, "a mark nobody can decode is not a mark"


def test_a_sweep_with_no_declines_prints_no_such_column(capsys) -> None:
    """The common case must not grow a column of zeroes and a legend nobody needs."""
    report._print_arms(
        [
            a_row(arm="full", rep=1, passed=True),
            a_row(arm="baseline", rep=1, passed=False, state="failed", turns=2),
        ]
    )
    out = capsys.readouterr().out

    assert "routed away" not in out


# ---------------------------------------------------------------------------
# And the second cause: a row whose commands reached outside the checkout
# ---------------------------------------------------------------------------
def a_row_that_escaped(*, arm: str, rep: int, passed: bool) -> dict:
    row = a_row(arm=arm, rep=rep, passed=passed, state="failed", turns=3, tool_calls=22)
    row["left_the_workspace"] = [
        "seq 62: references 'D:\\\\work\\\\forge' :: cd /d D:\\\\work\\\\forge && dir /b"
    ]
    return row


def test_escaping_the_checkout_is_the_second_cause() -> None:
    """`score` calls such a row "not evidence about the agent", so the report must too.

    Found on `not-in-the-checkout`: with the retriever removed the agent cannot reach the
    contract from its checkout, so it went looking on the host. Three of three `no-search`
    rows did it — a behaviour, not a coincidental reason string.
    """
    assert report._escaped(a_row_that_escaped(arm="no-search", rep=1, passed=False)) is True
    assert report._escaped(a_row(arm="full", rep=1, passed=True)) is False
    # Both causes are one concept for the arm table, and stay distinguishable in the footnote.
    assert report._aside(a_row_that_escaped(arm="no-search", rep=1, passed=False)) is True


def test_a_row_that_left_the_checkout_does_not_lower_the_arms_rate(capsys) -> None:
    # Two arms, because the table is a comparison and `_print_arms` returns early on one.
    report._print_arms(
        [
            a_row(arm="full", rep=1, passed=True),
            a_row_that_escaped(arm="full", rep=2, passed=False),
            a_row(arm="baseline", rep=1, passed=True),
        ]
    )
    out = capsys.readouterr().out

    assert "100%" in out, "the rate is over the rows that are evidence about the agent"
    assert "left the checkout" in out
    assert "measures the sandbox being probed" in out
