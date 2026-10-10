"""Tests for what already exists.

They say nothing about ``median``, because it does not exist yet. That is the
point of this task: the *new* test file the agent writes is the specification, and
a check that pins it away cannot tell a finished feature from an imagined one.
"""

from __future__ import annotations

import pytest

from series import mean, running_max


def test_mean_of_a_series() -> None:
    assert mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)


def test_mean_refuses_an_empty_series() -> None:
    with pytest.raises(ValueError):
        mean([])


def test_running_max() -> None:
    assert running_max([1.0, 3.0, 2.0, 5.0]) == pytest.approx([1.0, 3.0, 3.0, 5.0])
