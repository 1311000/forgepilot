"""The pricing rule, pinned as a rule rather than as a table of numbers.

Every case here is synthetic on purpose: the point is that the *verdict* follows from the
counts and the failure kinds, so a future run cannot get a better word by passing more reps
or by losing for a tidier reason.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from evals.pricing import (
    VERDICTS,
    cells_from_rows,
    main,
    price,
    summarise,
)


def _row(
    task: str,
    arm: str,
    rep: int,
    *,
    passed: bool,
    kinds: tuple[str, ...] = (),
) -> dict:
    return {
        "task": task,
        "arm": arm,
        "rep": rep,
        "passed": passed,
        "failure_kinds": list(kinds),
    }


def _arm(task: str, arm: str, passes: tuple[bool, ...], kinds: tuple[str, ...] = ("checks",)):
    """One arm's rows, with `kinds` on the failing ones."""
    return [
        _row(task, arm, index + 1, passed=passed, kinds=() if passed else kinds)
        for index, passed in enumerate(passes)
    ]


def _price(rows: list[dict], **kwargs):
    verdicts = price(rows, **kwargs)
    assert len(verdicts) == 1
    return verdicts[0]


def test_a_flip_on_a_behaviour_failure_is_priced() -> None:
    rows = _arm("t", "no-search", (False, False, True)) + _arm("t", "full", (True, True, True))
    verdict = _price(rows)
    assert verdict.verdict == "priced"
    assert "checks" in verdict.reason


def test_a_flip_without_recorded_kinds_is_priced_but_unclassified() -> None:
    rows = _arm("t", "no-search", (False, False, False), kinds=())
    rows += _arm("t", "full", (True, True, True))
    verdict = _price(rows)
    assert verdict.verdict == "priced_but_unclassified"
    assert "no failure kind" in verdict.reason


def test_a_flip_only_on_scratch_files_is_not_priced_as_behaviour() -> None:
    """The arm lost because it was untidy. That is a cost, not the answer."""
    rows = _arm("t", "no-repair", (False, False, False), kinds=("scratch", "cost"))
    rows += _arm("t", "full", (True, True, True))
    verdict = _price(rows)
    assert verdict.verdict == "priced_but_unclassified"
    assert "scratch" in verdict.reason


def test_a_narration_failure_does_not_price_a_mechanism() -> None:
    rows = _arm("t", "no-memory", (False, False, False), kinds=("narration",))
    rows += _arm("t", "full", (True, True, True))
    assert _price(rows).verdict == "priced_but_unclassified"


def test_no_flip_is_unpriced() -> None:
    rows = _arm("t", "no-memory", (True, True, True)) + _arm("t", "full", (True, True, True))
    verdict = _price(rows)
    assert verdict.verdict == "unpriced"
    assert "no flip" in verdict.reason


def test_the_arm_beating_the_whole_is_inverted() -> None:
    rows = _arm("t", "baseline", (True, True, True)) + _arm("t", "full", (False, True, False))
    verdict = _price(rows)
    assert verdict.verdict == "inverted"
    assert "beats the whole" in verdict.reason


def test_two_reps_cannot_price_anything() -> None:
    rows = _arm("t", "no-search", (False, False)) + _arm("t", "full", (True, True))
    verdict = _price(rows)
    assert verdict.verdict == "insufficient"
    assert "needs 3" in verdict.reason


def test_min_reps_is_a_parameter_because_a_run_may_be_given_more_budget() -> None:
    rows = _arm("t", "no-search", (False, False)) + _arm("t", "full", (True, True))
    assert _price(rows, min_reps=2).verdict == "priced"


def test_every_verdict_is_in_the_closed_set() -> None:
    rows = []
    for arm, passes in {
        "no-search": (False, False, True),
        "no-memory": (True, True, True),
        "baseline": (False, False, False),
        "no-pinning": (True, True, False),
    }.items():
        rows += _arm("t", arm, passes)
    rows += _arm("t", "full", (True, True, False))
    for verdict in price(rows):
        assert verdict.verdict in VERDICTS


def test_rows_are_grouped_by_task_and_sorted_by_rep() -> None:
    rows = [
        _row("b", "no-memory", 2, passed=True),
        _row("a", "no-memory", 3, passed=False, kinds=("checks",)),
        _row("a", "no-memory", 1, passed=True),
    ]
    grouped = cells_from_rows(rows)
    assert sorted(grouped) == ["a", "b"]
    assert [cell.rep for cell in grouped["a"]["no-memory"]] == [1, 3]


def test_the_cli_prints_a_table_and_can_print_json(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "rows.jsonl"
    rows = _arm("t", "no-search", (False, False, False)) + _arm("t", "full", (True, True, True))
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    assert main(["--results", str(path)]) == 0
    table = capsys.readouterr().out
    assert "no-search" in table and "priced" in table

    assert main(["--results", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["verdict"] == "priced"
    assert payload["armPassed"] == 0 and payload["fullPassed"] == 3


def test_the_cli_refuses_a_file_with_nothing_to_price(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text(json.dumps(_row("t", "full", 1, passed=True)) + "\n", encoding="utf-8")
    assert main(["--results", str(path)]) == 2
    assert "no arm to price" in capsys.readouterr().err


def test_missing_kinds_are_derived_from_the_failure_text() -> None:
    """Rows written before the field existed still carry the reason, and the classifier is pure."""
    rows = [
        {
            "task": "t",
            "arm": "no-search",
            "rep": 1,
            "passed": False,
            "failures": ["checks did not pass"],
        },
        {
            "task": "t",
            "arm": "no-search",
            "rep": 2,
            "passed": False,
            "failures": ["checks did not pass"],
        },
        {
            "task": "t",
            "arm": "no-search",
            "rep": 3,
            "passed": False,
            "failures": ["checks did not pass"],
        },
        {"task": "t", "arm": "full", "rep": 1, "passed": True, "failures": []},
        {"task": "t", "arm": "full", "rep": 2, "passed": True, "failures": []},
        {"task": "t", "arm": "full", "rep": 3, "passed": True, "failures": []},
    ]
    assert _price(rows).verdict == "priced"
    assert _price(rows, derive_missing_kinds=False).verdict == "priced_but_unclassified"


def test_the_summary_counts_verdicts_per_task() -> None:
    rows = _arm("a", "no-search", (False, False, False)) + _arm("a", "full", (True, True, True))
    rows += _arm("b", "no-memory", (True, True, True)) + _arm("b", "full", (True, True, True))
    counts = summarise(price(rows))
    assert counts["a"]["priced"] == 1
    assert counts["b"]["unpriced"] == 1
