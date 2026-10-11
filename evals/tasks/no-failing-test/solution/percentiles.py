"""The fix: one implementation, and it is the report's.

`percentiles.percentile` now *is* the house rule rather than a second opinion about
it. Delegating rather than copying the arithmetic is deliberate — a copy is a second
place for the convention to drift from, which is how this task exists in the first
place.
"""

from __future__ import annotations

from collections.abc import Sequence

from reporting import percentile_rank


def percentile(values: Sequence[float], p: float) -> float:
    """The p-th percentile, by the report's nearest-rank convention."""
    return percentile_rank(values, p)


def p50(values: Sequence[float]) -> float:
    return percentile(values, 50)


def p95(values: Sequence[float]) -> float:
    return percentile(values, 95)
