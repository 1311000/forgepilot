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


WITH_REGRESSIONS = SwebenchInstance(
    instance_id="astropy__astropy-14182",
    repo="astropy/astropy",
    base_commit="a5917978be",
    problem_statement="an unrelated failure",
    fail_to_pass=("astropy/io/ascii/tests/test_rst.py::test_rst_with_header_rows",),
    pass_to_pass=(
        "astropy/io/ascii/tests/test_rst.py::test_rst_without_header_rows",
        "astropy/io/ascii/tests/test_read.py::test_simple",
    ),
)


def test_the_regression_files_join_the_fix_files_once() -> None:
    assert G.pass_files(WITH_REGRESSIONS) == (
        "astropy/io/ascii/tests/test_rst.py",
        "astropy/io/ascii/tests/test_read.py",
    )
    # The fix file is already in the list; the union does not repeat it.
    assert G.all_test_files(WITH_REGRESSIONS) == (
        "astropy/io/ascii/tests/test_rst.py",
        "astropy/io/ascii/tests/test_read.py",
    )
    recipe = G.script_for(WITH_REGRESSIONS, None)
    assert "test_rst.py" in recipe
    assert "test_read.py" in recipe


def test_a_pass_to_pass_failure_is_unresolved_even_when_the_fix_landed() -> None:
    output = """
FAILED astropy/io/ascii/tests/test_read.py::test_simple - AssertionError
1 failed, 40 passed in 3.10s
"""
    report = G.report_for(WITH_REGRESSIONS, output, "none")
    assert report["f2p_failed"] == []
    assert report["p2p_failed"] == ["astropy/io/ascii/tests/test_read.py::test_simple"]
    assert report["verdict"] == "unresolved"


def test_a_failure_outside_both_lists_does_not_grade_the_environment() -> None:
    output = """
FAILED astropy/io/ascii/tests/test_ecsv.py::test_something_else - ConnectionError
1 failed, 40 passed in 3.10s
"""
    report = G.report_for(WITH_REGRESSIONS, output, "none")
    assert report["f2p_failed"] == []
    assert report["p2p_failed"] == []
    assert report["verdict"] == "resolved"
