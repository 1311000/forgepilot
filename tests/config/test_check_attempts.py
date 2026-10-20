"""``check_attempts``: a retry of the command, not of the model.

The key is easy to confuse with ``retry.max_attempts``, which is the *repair loop*
— how many model turns may follow a failure. These tests pin the distinction and
the refusals, so a typo cannot silently become a number of runs.
"""

from __future__ import annotations

import pytest

from forgepilot.config.errors import ConfigError
from forgepilot.config.project import MAX_CHECK_ATTEMPTS, project_from_mapping


def test_absent_means_one_run() -> None:
    assert project_from_mapping({}).check_attempts == 1


def test_two_is_accepted() -> None:
    assert project_from_mapping({"check_attempts": 2}).check_attempts == 2


def test_the_cap_is_where_the_error_says_it_is() -> None:
    assert MAX_CHECK_ATTEMPTS == 3
    assert project_from_mapping({"check_attempts": 3}).check_attempts == 3


@pytest.mark.parametrize("value", [0, 4, -1])
def test_out_of_range_is_refused_not_clamped(value: int) -> None:
    """A config that asks for 5 and gets 3 says something the harness does not do."""
    with pytest.raises(ConfigError, match="check_attempts must be between 1 and 3"):
        project_from_mapping({"check_attempts": value})


@pytest.mark.parametrize("value", [True, "two", 2.0])
def test_a_value_that_is_not_an_integer_is_refused(value: object) -> None:
    """``True`` is an int in Python; ``check_attempts: true`` is a typo, not two runs."""
    with pytest.raises(ConfigError, match="check_attempts must be an integer"):
        project_from_mapping({"check_attempts": value})


def test_it_is_a_different_key_from_the_repair_loop_budget() -> None:
    """Two retry knobs, two meanings — one asks the command, one asks the model."""
    project = project_from_mapping(
        {"check_attempts": 2, "retry": {"max_attempts": 5}},
    )

    assert project.check_attempts == 2
    assert project.retry.max_attempts == 5
