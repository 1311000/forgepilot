"""Series helpers for the reporting job."""

from __future__ import annotations

from collections.abc import Sequence


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean. Raises on an empty series rather than returning 0."""
    if not values:
        raise ValueError("mean of an empty series is undefined, not zero")
    return sum(values) / len(values)


def median(values: Sequence[float]) -> float:
    """The middle value; the mean of the two middle values for an even count.

    Raises on an empty series, for the same reason `mean` does — there is no
    middle of nothing, and returning 0 would put a fabricated data point on a
    dashboard.
    """
    if not values:
        raise ValueError("median of an empty series is undefined, not zero")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def running_max(values: Sequence[float]) -> list[float]:
    """The largest value seen so far, at each position."""
    out: list[float] = []
    best: float | None = None
    for value in values:
        best = value if best is None else max(best, value)
        out.append(best)
    return out
