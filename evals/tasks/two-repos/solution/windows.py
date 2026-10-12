"""Rolling windows for the weekly dashboard.

Used by the dashboard job to turn raw daily readings into the numbers that end up
on the weekly chart.

⚠️ **This module is a copy, and it copied the bug.** The window arithmetic came
from `metrics-stats` when the dashboard split into its own repository, and the
same off-by-one came with it: the last full window of a series is dropped. One
ticket covers both repositories because one defect is in both of them.
"""

from __future__ import annotations

from collections.abc import Sequence


def weekly_average(readings: Sequence[float], days: int) -> list[float]:
    """The average of each consecutive run of ``days`` readings.

    ``[1, 2, 3, 4]`` with ``days=2`` gives ``[1.5, 2.5, 3.5]`` — three windows:
    ``(1,2)``, ``(2,3)`` and ``(3,4)``.
    """
    if days <= 0:
        raise ValueError("days must be positive")
    if days > len(readings):
        return []

    averages: list[float] = []
    for index in range(len(readings) - days + 1):
        chunk = readings[index : index + days]
        averages.append(sum(chunk) / days)
    return averages


def latest(readings: Sequence[float]) -> float:
    """The most recent reading. Raises on an empty series rather than returning 0."""
    if not readings:
        raise ValueError("latest of an empty series is undefined, not zero")
    return readings[-1]
