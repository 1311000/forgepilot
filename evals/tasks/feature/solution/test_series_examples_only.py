"""The test an agent writes when it treats the ticket as the specification.

The base tests, plus the ticket's two examples and nothing else — so it agrees exactly with
`series_median_of_the_examples.py`, and the pair is green while the rule the ticket states
("even counts average the two middle values") is absent from the module for every even
count the ticket did not happen to name.

**Script input, never shipped code.**
"""

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


def test_median_of_an_odd_series() -> None:
    assert median([3, 1, 2]) == 2.0


def test_median_of_an_even_series() -> None:
    assert median([4, 1, 3, 2]) == 2.5
