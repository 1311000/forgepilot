"""Small statistics helpers for the reporting pipeline.

Used by the metrics job to turn raw sample series into the numbers that end up on
the dashboard.
"""

from __future__ import annotations

from collections.abc import Sequence


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean. Raises on an empty series rather than returning 0."""
    if not values:
        raise ValueError("mean of an empty series is undefined, not zero")
    return sum(values) / len(values)


def moving_average(values: Sequence[float], window: int) -> list[float]:
    """The average of each consecutive run of ``window`` values.

    ``[1, 2, 3, 4]`` with ``window=2`` gives ``[1.5, 2.5, 3.5]`` — three windows:
    ``(1,2)``, ``(2,3)`` and ``(3,4)``.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    if window > len(values):
        return []

    averages: list[float] = []
    for index in range(len(values) - window + 1):
        chunk = values[index : index + window]
        averages.append(sum(chunk) / window)
    return averages


def running_max(values: Sequence[float]) -> list[float]:
    """The largest value seen so far, at each position."""
    out: list[float] = []
    best: float | None = None
    for value in values:
        best = value if best is None else max(best, value)
        out.append(best)
    return out
