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

    ⚠️ **Built-in ``round`` is not this function.** Python's ``round`` is
    half-to-**even**: ``round(2.5)`` is 2 and ``round(0.5)`` is 0, because it breaks
    ties toward the even neighbour rather than away from zero. That is the right
    rule for statistical work and the wrong one for money, and the difference only
    shows on an exact half — which is exactly the case an invoice hits whenever a
    unit price has a half cent in it.

    The two branches are both ``floor``-shaped on purpose: adding a half and
    flooring rounds up for a positive value, and subtracting a half and ceiling
    rounds away from zero for a negative one. Using ``floor`` on both would send
    ``-2.5`` to ``-3`` and ``-2.4`` to ``-3`` as well.
    """
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def truncate(value: float) -> int:
    """Drop the fraction. Used for accruals, which must never be overstated."""
    return math.floor(value) if value >= 0 else math.ceil(value)
