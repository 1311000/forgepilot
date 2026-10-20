"""`project.yaml: max_tokens` caps one reply, and `budget.max_tokens` caps the task.

The two keys sit next to each other in the same file and read alike, so the test
that matters is the one that says they are *different numbers on different
paths*: a project that raises the reply cap must not thereby raise its budget.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from forgepilot.config.project import project_from_mapping


def test_no_max_tokens_leaves_the_loop_default_alone() -> None:
    project = project_from_mapping({"name": "svc", "repos": ["git.corp/team/a"]})
    assert project.max_tokens is None


def test_max_tokens_is_read_and_does_not_touch_the_budget() -> None:
    project = project_from_mapping(
        {
            "name": "svc",
            "repos": ["git.corp/team/a"],
            "max_tokens": 16_000,
            "budget": {"max_tokens": 40_000},
        }
    )
    assert project.max_tokens == 16_000
    assert project.budget.max_tokens == 40_000, (
        "raising the reply cap moved the task budget, so the two keys are one key"
    )


def test_the_request_default_mirrors_the_loop() -> None:
    """A copy that is not checked is a copy that drifts."""
    from forgepilot.orchestration.modes.task import DEFAULT_REQUEST_TOKENS
    from forgepilot.runtime.loop.turn import run_turn

    default = inspect.signature(run_turn).parameters["max_tokens"].default
    assert default == DEFAULT_REQUEST_TOKENS, (
        f"run_turn defaults to {default} and task mode passes "
        f"{DEFAULT_REQUEST_TOKENS}: a project that declares no cap would get a "
        f"different one depending on which path ran it"
    )


def test_a_project_file_carries_it(tmp_path: Path) -> None:
    from forgepilot.config.project import load_project

    path = tmp_path / "project.yaml"
    path.write_text("name: svc\nrepos: [git.corp/team/a]\nmax_tokens: 2048\n", encoding="utf-8")
    assert load_project(path).max_tokens == 2048


def test_a_non_numeric_cap_is_refused() -> None:
    with pytest.raises((ValueError, TypeError)):
        project_from_mapping({"name": "svc", "repos": ["git.corp/team/a"], "max_tokens": "lots"})
