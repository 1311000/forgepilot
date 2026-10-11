"""Percentiles for the dashboard's sparkline panels.

Written later than `reporting.py`, by someone who did not know the report already
had a convention, and it interpolates between the two neighbouring values. The
ticket reports the disagreement and names which side is authoritative; nothing in
`tests/` fails, because every case the dashboard's own suite covers is one where
the two methods happen to agree.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def percentile(values: Sequence[float], p: float) -> float:
    """The p-th percentile, interpolating between the two neighbouring values."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of an empty series is undefined")
    if not 0 < p <= 100:
        raise ValueError("p must be in (0, 100]")
    position = (len(ordered) - 1) * (p / 100)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (position - low))


def p50(values: Sequence[float]) -> float:
    return percentile(values, 50)


def p95(values: Sequence[float]) -> float:
    return percentile(values, 95)
