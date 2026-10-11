"""The percentile the weekly report has always used.

⚠️ **This module is the convention, not one of two opinions.** `percentiles.py`
computes the same statistic a second way; where the two disagree, this one is what
the published numbers were computed with — including the numbers in contracts that
have already been signed. That is why the ticket says this side wins, and it is why
the test beside it pins the even-length case rather than leaving it to a comment.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def percentile_rank(values: Sequence[float], p: float) -> float:
    """The **nearest-rank** percentile: the ⌈p% × n⌉-th smallest value.

    No interpolation, ever. With four values and ``p=50`` this is the *second*
    value, not the average of the second and third — which is exactly what the two
    modules in this repository disagree about.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of an empty series is undefined")
    if not 0 < p <= 100:
        raise ValueError("p must be in (0, 100]")
    return float(ordered[math.ceil(p / 100 * len(ordered)) - 1])
