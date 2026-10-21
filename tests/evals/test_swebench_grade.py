"""The grader's two pure functions, and the rule that keeps a silent run from passing."""

from __future__ import annotations

import sys
from pathlib import Path

FORGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE / "tools"))
sys.path.insert(0, str(FORGE))

import swebench_grade as G  # noqa: E402

from evals.swebench import SwebenchInstance, verdict  # noqa: E402

INSTANCE = SwebenchInstance(
    instance_id="astropy__astropy-12907",
    repo="astropy/astropy",
    base_commit="d16bfe05a7",
    problem_statement="separable loses the operator",
    fail_to_pass=(
        "astropy/modeling/tests/test_separable.py::test_separable[compound_model6-result6]",
        "astropy/modeling/tests/test_separable.py::test_separable[compound_model9-result9]",
        "astropy/modeling/tests/test_separable.py::test_separable[other-result]",
    ),
)


def test_the_files_are_deduplicated_in_order() -> None:
    assert G.test_files(INSTANCE) == ("astropy/modeling/tests/test_separable.py",)


def test_failed_ids_are_read_from_the_summary_lines() -> None:
    output = """
FAILED astropy/modeling/tests/test_separable.py::test_separable[compound_model6-result6] - AssertionError
FAILED astropy/modeling/tests/test_separable.py::test_separable[compound_model9-result9] - AssertionError
2 failed, 13 passed in 2.49s
"""
    assert G.parse_failed(output) == (
        "astropy/modeling/tests/test_separable.py::test_separable[compound_model6-result6]",
        "astropy/modeling/tests/test_separable.py::test_separable[compound_model9-result9]",
    )


def test_a_green_run_yields_resolved_and_a_silent_one_does_not() -> None:
    assert verdict(INSTANCE, G.parse_failed("15 passed in 1.52s")) == "resolved"
    # An empty id list is also what a run nobody could read produces, so it is only
    # `resolved` when the runner actually said something.
    assert verdict(INSTANCE, (), runner_told_us=False) == "unknown"
