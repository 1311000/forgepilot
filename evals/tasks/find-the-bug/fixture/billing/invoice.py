"""What the customer is charged, and what the customer is shown.

⚠️ This module is where the ticket points — «相关文件：invoice.py» — and it is not
where the defect is. Everything here is arithmetically correct; it is the *rule* it
delegates to that is not. An agent that trusts the ticket edits this file and
changes nothing about the failing assertion.
"""

from __future__ import annotations

from collections.abc import Sequence

from billing.money import to_cents
from billing.rounding import round_half_up

TAX_RATE = 0.05


def line_total_cents(unit_price: float, quantity: int) -> int:
    """One line, rounded to cents once, at the end."""
    return to_cents(unit_price * quantity)


def tax_cents(subtotal: float) -> int:
    return round_half_up(subtotal * TAX_RATE * 100)


def invoice_total_cents(lines: Sequence[tuple[float, int]]) -> int:
    """The charges plus tax, in cents.

    The subtotal is rounded **once**, on the summed amount rather than per line —
    rounding each line first and adding the results drifts by a cent on an invoice
    with enough lines on it.
    """
    subtotal = sum(unit_price * quantity for unit_price, quantity in lines)
    return to_cents(subtotal) + tax_cents(subtotal)
