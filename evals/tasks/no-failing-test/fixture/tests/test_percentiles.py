"""The dashboard's tests for its own percentiles.

⚠️ **Every case here agrees between the two methods**, and that is the whole point
of the task rather than an oversight in the suite. With **five** values, p ∈
{25, 50, 75} lands the interpolated position exactly on a data point
(``(5-1) × p/100`` is 1, 2 or 3) and ``p=100`` is the maximum either way; with
**one** value the two are trivially identical. So this file is green before the fix
and green after it, and an agent that treats it as the specification has no way to
know which method it was supposed to implement.

The held-out input is where the two disagree. See `probes/`.
"""

from __future__ import annotations

import pytest

from percentiles import p50, p95, percentile

FIVE = [1.0, 2.0, 3.0, 4.0, 5.0]


@pytest.mark.parametrize(("p", "expected"), [(25, 2.0), (50, 3.0), (75, 4.0), (100, 5.0)])
def test_five_values_land_on_data_points(p: float, expected: float) -> None:
    assert percentile(FIVE, p) == expected


def test_the_median_of_five_is_three() -> None:
    assert p50(FIVE) == 3.0


def test_one_value() -> None:
    assert p50([7.0]) == 7.0
    assert p95([7.0]) == 7.0


def test_an_empty_series_is_an_error() -> None:
    with pytest.raises(ValueError):
        percentile([], 50)


@pytest.mark.parametrize("p", [0, -1, 101])
def test_p_outside_the_range_is_an_error(p: float) -> None:
    with pytest.raises(ValueError):
        percentile([1.0, 2.0], p)
