"""The published report's own tests — and the one that pins which method is the house one.

⚠️ **This test is load-bearing twice.** It is the visible statement of the
convention, so an agent that reads the suite instead of guessing can learn the rule
without reading a word of prose; and it is the guard against "unifying" the two
modules in the *wrong* direction, which would make the visible suite fail rather
than the held-out input.
"""

from __future__ import annotations

import pytest

from reporting import percentile_rank


def test_the_even_length_case_is_not_an_average() -> None:
    """Four values at ``p=50``: nearest-rank takes the *second*. Interpolating
    returns 2.5, and that difference is what the ticket is about."""
    assert percentile_rank([1.0, 2.0, 3.0, 4.0], 50) == 2.0


def test_p100_is_the_maximum() -> None:
    assert percentile_rank([1.0, 9.0, 4.0], 100) == 9.0


def test_one_value_is_its_own_every_percentile() -> None:
    assert percentile_rank([7.0], 1) == 7.0
    assert percentile_rank([7.0], 99) == 7.0


def test_an_empty_series_is_an_error() -> None:
    with pytest.raises(ValueError):
        percentile_rank([], 50)


@pytest.mark.parametrize("p", [0, -1, 101])
def test_p_outside_the_range_is_an_error(p: float) -> None:
    with pytest.raises(ValueError):
        percentile_rank([1.0, 2.0], p)
