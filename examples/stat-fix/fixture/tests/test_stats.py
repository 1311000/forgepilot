"""Tests for the statistics helpers.

These are written against the *documented* behaviour, so the two moving-average
tests fail together on the seeded defect. Two failures from one bug is the normal
shape of a real report — the point of the fixture is that the checks are an
honest oracle, not a second copy of the bug.
"""

from __future__ import annotations

import pytest

from stats import mean, moving_average, running_max


def test_mean_of_a_series() -> None:
    assert mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)


def test_mean_refuses_an_empty_series() -> None:
    with pytest.raises(ValueError):
        mean([])


def test_moving_average_over_a_series() -> None:
    assert moving_average([1.0, 2.0, 3.0, 4.0], 2) == pytest.approx([1.5, 2.5, 3.5])


def test_moving_average_covers_every_full_window() -> None:
    """Four values with a window of two contain **three** windows:
    (1,2), (2,3) and (3,4). The dashboard relies on the count matching."""
    assert len(moving_average([1.0, 2.0, 3.0, 4.0], 2)) == 3


def test_moving_average_of_exactly_one_window() -> None:
    assert moving_average([1.0, 2.0, 3.0], 3) == pytest.approx([2.0])


def test_moving_average_rejects_a_nonpositive_window() -> None:
    with pytest.raises(ValueError):
        moving_average([1.0], 0)


def test_moving_average_of_a_series_shorter_than_the_window_is_empty() -> None:
    assert moving_average([1.0], 3) == []


def test_running_max() -> None:
    assert running_max([1.0, 3.0, 2.0, 5.0]) == pytest.approx([1.0, 3.0, 3.0, 5.0])
