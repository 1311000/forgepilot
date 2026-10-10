"""Accruals — the one place in the package that must **not** round up.

An accrual is an estimate of money not yet owed; overstating one is a different
mistake from understating an invoice, which is why this module reaches for
``truncate`` and deliberately not for the usual rule.
"""

from __future__ import annotations

from collections.abc import Sequence

from billing.rounding import truncate


def accrued_cents(estimates: Sequence[float]) -> int:
    """The sum of ``estimates`` in cents, rounded **down**.

    ``truncate`` rather than the shared rule on purpose: a half cent of estimated
    liability is not owed, so it is not accrued.
    """
    return sum(truncate(estimate * 100) for estimate in estimates)
