"""Rounding helpers for the metrics pipeline."""

from __future__ import annotations

THRESHOLD = 0.5


def bucket(value: float) -> int:
    """Round to the nearest whole number, half away from zero.

    The bug the ticket is about: ``int()`` truncates toward zero, so 2.9 becomes
    2 and -2.9 becomes -2. `THRESHOLD` in the project root is the *configured*
    half, and someone has decided the fix belongs there.
    """
    return int(value)
