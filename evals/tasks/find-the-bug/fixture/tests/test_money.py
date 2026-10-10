"""Amounts, and the accrual rule that deliberately differs from them.

Everything here passes on the seeded defect, which is the point: the only red test
in the repository is at the *invoice* level, and it names a value rather than a
place. An agent that runs the suite learns that something is wrong with a half
cent; it does not learn where.
"""

from __future__ import annotations

from billing.ledger import accrued_cents
from billing.money import format_amount, to_cents


def test_an_amount_scales_to_cents() -> None:
    assert to_cents(1.20) == 120


def test_a_fraction_of_a_cent_is_rounded_away() -> None:
    assert to_cents(1.234) == 123


def test_an_amount_is_rendered_with_two_decimals() -> None:
    assert format_amount(1.2) == "1.20"
    assert format_amount(1.239) == "1.24"


def test_accruals_round_down_rather_than_to_nearest() -> None:
    """``2.005`` accrues as 200 cents, not 201 — an accrual is an estimate of money
    not yet owed, and half a cent of it is not owed."""
    assert accrued_cents([2.005]) == 200


def test_accruals_sum_after_truncating_each_estimate() -> None:
    assert accrued_cents([0.006, 0.006]) == 0
