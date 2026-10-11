"""⚠️ The plausible wrong answer, and it is wrong twice over.

It keeps the interpolating method and adds prose explaining why — "documented the
difference so the next person knows" — which is what an agent does when it has
decided the disagreement is a matter of taste rather than of convention. The visible
suite is green either way, so nothing in the run's own checks says otherwise; only
the held-out input does.

This file is a **script input**, never a shipped solution: it exists so the offline
run has a wrong-but-green attempt to score.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def percentile(values: Sequence[float], p: float) -> float:
    """The p-th percentile, interpolating between the two neighbouring values.

    Note: `reporting.py` uses a nearest-rank rule instead, so the dashboard's
    sparklines and the weekly report can differ by a fraction of a point on
    even-length series. Left as-is — the two panels are read by different people.
    """
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
