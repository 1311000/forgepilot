"""Invoice-level tests.

⚠️ **The failure this task turns on names a value, not a place.** ``assert 112 ==
113`` has no traceback frame inside the package: the rounding rule that produced
the 112 is three calls down, and nothing in the assertion points at it. Finding it
is the task.
"""

from __future__ import annotations

from billing.invoice import invoice_total_cents, line_total_cents, tax_cents


def test_a_line_with_no_half_cent_rounds_to_the_nearest() -> None:
    assert line_total_cents(1.20, 3) == 360


def test_a_half_cent_on_a_line_rounds_up() -> None:
    """1.125 is exactly representable, so this is a question about the *rule* and
    not about binary fractions. The half goes away from zero, which for money means
    up."""
    assert line_total_cents(1.125, 1) == 113


def test_tax_is_charged_on_the_cent() -> None:
    assert tax_cents(10.00) == 50


def test_an_invoice_total_is_charges_plus_tax() -> None:
    assert invoice_total_cents([(1.00, 2)]) == 200 + 10


def test_a_half_cent_in_the_subtotal_reaches_the_total() -> None:
    """The end-to-end version of the same question, and the one a reviewer would
    actually notice: the customer is shown a total one cent lower than the lines
    add up to.

    119 is 113 of charges (rounded up from 112.5) plus 6 of tax at 5%.
    """
    assert invoice_total_cents([(1.125, 1)]) == 119
