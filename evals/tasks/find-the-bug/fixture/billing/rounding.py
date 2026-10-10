"""The rounding rules for money, in one place on purpose.

The ledger and the invoice renderer have to agree on a single definition. A total
computed one way and displayed another disagrees by a cent, and the cent always
surfaces on a customer's statement rather than in a test.
"""

from __future__ import annotations

import math


def round_half_up(value: float) -> int:
    """``value`` rounded to the nearest whole number, halves **away from zero**.

    Callers scale to the unit they care about first — money is rounded by scaling
    to cents, so ``2.5`` here means two and a half cents.
    """
    return round(value)


def truncate(value: float) -> int:
    """Drop the fraction. Used for accruals, which must never be overstated."""
    return math.floor(value) if value >= 0 else math.ceil(value)
