"""An arm is a configuration change, and it has to reach the run.

⚠️ **Three of the five arms never did.** `Arm.project` — the field `no-search`,
`no-pinning` and `no-repair` are *entirely made of* — was read by nothing.
`write_project` was called without it, so those three arms ran the baseline
configuration under a different name.

The first ablation said:

    full 92%   no-search 100%   no-pinning 100%   no-repair 100%   tight-window 75%

and the reading given at the time was "the tasks are too easy for these mechanisms
to matter". **Three of those four rates were the same run repeated.** `tight-window`
was the one arm that behaved differently, because it is the only one that uses
`patches` — and `boot()` did apply those. A mechanism whose absence produces no
difference and a mechanism that was never switched off look identical from the arm
table, and the only thing that tells them apart is reading the call that builds the
run's configuration.

These tests are that reading, made executable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import arms as arm_module  # noqa: E402
import harness as H  # noqa: E402


@pytest.fixture
def task():
    return H.load_task("bug-fix")


# ---------------------------------------------------------------------------
# Every arm changes something, and the right thing
# ---------------------------------------------------------------------------
def test_every_arm_changes_something() -> None:
    """An arm that changes nothing is not an arm. `full` is the one exception, and
    it is the baseline rather than an ablation."""
    for name, arm in arm_module.ARMS.items():
        if name == "full":
            continue
        assert arm.project or arm.patches, (
            f"arm {name!r} switches nothing off, so comparing it against `full` "
            f"compares the baseline with itself"
        )


def test_the_no_search_arm_removes_the_search_tool(task) -> None:
    arm = arm_module.arm("no-search")
    merged = H.project_mapping(task, arm=arm)

    for category, tools in merged["tool_sets"].items():
        assert "code_search" not in tools, f"{category} still offers code_search"


def test_the_no_pinning_arm_empties_the_test_paths(task) -> None:
    """`[]` is the explicit "pin nothing" — a different statement from omitting the
    key, which would inherit the example's `tests/`."""
    arm = arm_module.arm("no-pinning")
    merged = H.project_mapping(task, arm=arm)

    assert merged["test_paths"] == []


def test_the_no_repair_arm_ends_after_one_attempt(task) -> None:
    arm = arm_module.arm("no-repair")
    merged = H.project_mapping(task, arm=arm)

    assert merged["retry"]["max_attempts"] == 0


# ---------------------------------------------------------------------------
# And it does not leak into the expectation
# ---------------------------------------------------------------------------
def test_the_expectation_does_not_see_the_arm(task) -> None:
    """⚠️ **The distinction the old docstring drew correctly and then applied to
    both.** What counts as a test path is the *task's* declaration: letting
    `no-pinning` empty `test_paths` inside the expectation would make the task fail
    for a reason it does not measure, and the arm's whole result would be an
    artefact of its own scoring.
    """
    arm = arm_module.arm("no-pinning")

    assert H.project_mapping(task)["test_paths"] != []
    assert H.project_mapping(task, arm=arm)["test_paths"] == []
    assert H.project_mapping(task) == H.project_mapping(task, arm=None)


# ---------------------------------------------------------------------------
# And it reaches the file the run actually loads
# ---------------------------------------------------------------------------
def test_the_configuration_written_for_a_run_carries_the_arm(task, tmp_path, monkeypatch) -> None:
    """The end of the chain, and the link that was missing.

    Asserted on the **loaded** project rather than on the YAML text: `load_project`
    is what the run uses, and a mapping that is right on paper and wrong after
    expansion is the defect this file already records once.
    """
    monkeypatch.setenv("FORGEPILOT_PYTHON", sys.executable)

    plain = H.write_project(task, tmp_path / "plain")
    ablated = H.write_project(
        task, tmp_path / "ablated", arm=arm_module.arm("no-repair")
    )

    assert plain.retry.max_attempts != 0
    assert ablated.retry.max_attempts == 0
    assert H.write_project.__doc__ and "arm" in H.write_project.__doc__
