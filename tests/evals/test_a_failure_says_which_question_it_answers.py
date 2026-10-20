"""A failure is not just red: it answers a question, and the answer is recorded.

M23 measured 20 of 32 failed cells as notes the agent left in the tree, and P4
found `out-of-scope` inverting between sweeps because its only check is a
sentence. Both facts were *in* the results files and neither was *readable*: the
change limit counted debris beside edits, and the report added narration to
behaviour. These tests pin the two separations and the one limit that must not
move with them — a real edit over the limit is still a failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

FORGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import harness as H  # noqa: E402


def a_task(**expect: object) -> H.EvalTask:
    """The smallest task `score` will accept: a name and the expectations."""
    return H.EvalTask(
        name="a-task",
        directory=FORGE / "evals" / "tasks" / "bug-fix",
        fixture=FORGE / "evals" / "tasks" / "bug-fix" / "fixture",
        ticket={},
        expect=dict(expect),
    )


def a_row(**overrides: object) -> dict[str, object]:
    """A passing row for `bug-fix`, with the noisy keys defaulted."""
    row: dict[str, object] = {
        "state": "ready",
        "checks_passed": True,
        "changed": [],
        "tool_calls": 3,
        "turns": 1,
        "classifier_calls": 1,
        "agent_text": "fixed it",
        "probes": [],
    }
    row.update(overrides)
    return row


# --- the debris the matrix measured, named exactly ---------------------------


@pytest.mark.parametrize(
    "path",
    [
        "_commit_msg.txt",
        "BUG2_MSG.txt",
        "_bug2_msg.txt",
        "BUG2_COMMIT_MSG.txt",
        "_repro.py",
        "repro.py",
        "_scratch_repro.py",
        "_check.py",
        "_msg.txt",
        "_tmp_commit_msg.txt",
    ],
)
def test_the_notes_the_matrix_saw_are_debris(path: str) -> None:
    assert H.scratch_paths([path]) == [path]


@pytest.mark.parametrize("path", ["stats.py", "reporting/windows.py", "tests/test_limits.py"])
def test_a_delivery_is_not_debris(path: str) -> None:
    assert H.scratch_paths([path]) == []


def test_the_heuristic_has_a_stated_blind_spot() -> None:
    """`verify_fix.py` is debris by intent and is not caught by a name.

    Written down rather than papered over: the alternative is a smarter guess
    that would also swallow real files, and the row still records every path.
    """
    assert H.scratch_paths(["verify_fix.py"]) == []


# --- the limit counts edits, and debris has its own sentence ----------------


def test_debris_does_not_count_against_the_change_limit() -> None:
    task = a_task(max_changed_paths=2)
    row = a_row(
        changed=["stats.py", "reporting/windows.py", "_commit_msg.txt", "BUG2_MSG.txt"],
    )
    assert H.score(row, task) == []


def test_a_real_edit_over_the_limit_is_still_a_failure() -> None:
    task = a_task(max_changed_paths=2)
    row = a_row(changed=["stats.py", "reporting/windows.py", "settings.py"])
    failures = H.score(row, task)
    assert failures == ["3 paths changed, limit 2"]


def test_debris_can_be_a_failure_of_its_own_kind() -> None:
    task = a_task(max_scratch_paths=0)
    row = a_row(changed=["stats.py", "_commit_msg.txt", "BUG2_MSG.txt"])
    failures = H.score(row, task)
    assert len(failures) == 1
    assert "left 2 scratch file(s) in the tree" in failures[0]
    assert "_commit_msg.txt" in failures[0]
    assert H.failure_kind(failures[0]) == "scratch"


# --- a failure carries the question it answers ------------------------------


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("the agent never mentioned 'settings.py', so a reader cannot tell", "narration"),
        ("3 paths changed, limit 2", "changed"),
        ("checks did not pass", "checks"),
        ("state 'failed' not in ['ready', 'delivered']", "state"),
        ("no delivery was attempted for ['git.local/demo/other']", "delivery"),
        ("31 tool calls, limit 25", "cost"),
        ("the held-out probe 'x' failed: ...", "probe"),
        ("left 2 scratch file(s) in the tree, limit 0: a.txt", "scratch"),
        ("something nobody classified", "other"),
    ],
)
def test_a_failure_kind_is_the_question_it_answers(reason: str, kind: str) -> None:
    assert H.failure_kind(reason) == kind


def test_an_out_of_scope_failure_is_narration_and_says_so() -> None:
    """The P4 inversion, in one assertion: the check is a sentence, not a change."""
    task = a_task(reports=["settings.py"])
    row = a_row(agent_text="done")
    failures = H.score(row, task)
    assert len(failures) == 1
    assert H.failure_kind(failures[0]) == "narration"


# --- and the report reads the kinds rather than re-deriving them ------------


def _report():
    import importlib.util

    spec = importlib.util.spec_from_file_location("eval_report", FORGE / "evals" / "report.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_report_keeps_behaviour_and_narration_apart() -> None:
    report = _report()
    rows = [
        {
            "task": "a",
            "arm": "full",
            "passed": False,
            "failures": ["checks did not pass"],
            "failure_kinds": ["checks"],
        },
        {
            "task": "b",
            "arm": "full",
            "passed": False,
            "failures": ["the agent never mentioned 'settings.py'"],
            "failure_kinds": ["narration"],
        },
    ]
    reasons = report._reasons_of(rows)
    assert reasons[("checks", "checks did not pass")] == 1
    assert reasons[("narration", "the agent never mentioned 'settings.py'")] == 1


def test_a_row_from_before_the_kinds_existed_says_unknown() -> None:
    report = _report()
    reasons = report._reasons_of(
        [{"task": "a", "arm": "full", "passed": False, "failures": ["a reason"]}]
    )
    assert reasons[("?", "a reason")] == 1


def test_the_runner_records_both_facts_in_the_row(tmp_path: Path) -> None:
    """The row is the evidence; the report must not have to guess from a sentence."""
    row = {
        "changed": ["stats.py", "_commit_msg.txt"],
        "state": "ready",
        "checks_passed": True,
        "tool_calls": 3,
        "turns": 1,
        "classifier_calls": 1,
        "agent_text": "fixed",
        "probes": [],
    }
    scratch = H.scratch_paths(row["changed"])
    assert scratch == ["_commit_msg.txt"]
    # And a row on disk stays JSON, which is what makes it readable a year later.
    assert json.loads(json.dumps({"scratch": scratch})) == {"scratch": ["_commit_msg.txt"]}
