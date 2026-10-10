"""Amounts, in the one representation the rest of the package uses internally."""

from __future__ import annotations

from billing.rounding import round_half_up


def to_cents(amount: float) -> int:
    """``amount`` as a whole number of cents.

    Scaling to cents before rounding is deliberate: rounding the dollars and then
    multiplying loses the half-cent that the rule exists to decide.
    """
    return round_half_up(amount * 100)


def format_amount(amount: float) -> str:
    """The customer-facing rendering, two decimals, always."""
    return f"{to_cents(amount) / 100:.2f}"
