"""Tests for what already exists, plus the feature this task asked for."""

from __future__ import annotations

import pytest

from series import mean, median, running_max


def test_mean_of_a_series() -> None:
    assert mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)


def test_mean_refuses_an_empty_series() -> None:
    with pytest.raises(ValueError):
        mean([])


def test_running_max() -> None:
    assert running_max([1.0, 3.0, 2.0, 5.0]) == pytest.approx([1.0, 3.0, 3.0, 5.0])


def test_median_of_an_odd_count() -> None:
    assert median([3.0, 1.0, 2.0]) == pytest.approx(2.0)


def test_median_of_an_even_count_averages_the_middle_two() -> None:
    assert median([4.0, 1.0, 3.0, 2.0]) == pytest.approx(2.5)


def test_median_refuses_an_empty_series() -> None:
    with pytest.raises(ValueError):
        median([])
