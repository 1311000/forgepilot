"""Rounding helpers for the metrics pipeline."""

from __future__ import annotations

THRESHOLD = 0.5


def bucket(value: float) -> int:
    """Round to the nearest whole number, half away from zero.

    ``round()`` is deliberately not used: Python rounds halves to *even*, so
    ``round(2.5)`` is 2 and ``round(-2.5)`` is -2, which is the same surprise the
    truncation produced for a different reason.
    """
    return int(value + THRESHOLD) if value >= 0 else -int(-value + THRESHOLD)
