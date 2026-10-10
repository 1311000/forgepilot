"""Series helpers for the reporting job.

Deliberately missing ``median``: the ticket asks for it, and the whole point of
this task is that the *new* tests are the specification for something that does
not exist yet.
"""

from __future__ import annotations

from collections.abc import Sequence


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean. Raises on an empty series rather than returning 0."""
    if not values:
        raise ValueError("mean of an empty series is undefined, not zero")
    return sum(values) / len(values)


def running_max(values: Sequence[float]) -> list[float]:
    """The largest value seen so far, at each position."""
    out: list[float] = []
    best: float | None = None
    for value in values:
        best = value if best is None else max(best, value)
        out.append(best)
    return out
