"""Tests for the dashboard's rolling windows.

These are written against the *documented* behaviour, so the `weekly_average`
tests fail together on the seeded off-by-one — the same shape as
`metrics-stats`' own suite, because it is the same defect.
"""

from __future__ import annotations

import pytest

from reporting.windows import latest, weekly_average


def test_weekly_average_over_a_series() -> None:
    assert weekly_average([1.0, 2.0, 3.0, 4.0], 2) == pytest.approx([1.5, 2.5, 3.5])


def test_weekly_average_covers_every_full_window() -> None:
    """Four readings with a window of two contain **three** windows:
    (1,2), (2,3) and (3,4). The weekly chart relies on the count matching."""
    assert len(weekly_average([1.0, 2.0, 3.0, 4.0], 2)) == 3


def test_weekly_average_of_exactly_one_window() -> None:
    assert weekly_average([1.0, 2.0, 3.0], 3) == pytest.approx([2.0])


def test_weekly_average_rejects_a_nonpositive_window() -> None:
    with pytest.raises(ValueError):
        weekly_average([1.0], 0)


def test_weekly_average_of_a_series_shorter_than_the_window_is_empty() -> None:
    assert weekly_average([1.0], 3) == []


def test_latest() -> None:
    assert latest([1.0, 3.0, 2.0]) == pytest.approx(2.0)


def test_latest_refuses_an_empty_series() -> None:
    with pytest.raises(ValueError):
        latest([])
