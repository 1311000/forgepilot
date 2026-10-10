"""Tests for the paging arithmetic.

⚠️ **Three of these are what make the ticket's single example insufficient**, and
that is the point of the task. The ticket reports ``page_count(11, 10)`` and the
obvious repair — ``total // per_page + 1`` — satisfies it exactly. It also turns
``page_count(10, 10)`` into 2 and ``page_count(0, 10)`` into 1, which is why an
agent that writes the fix without running the suite has to come back and try
again. That second attempt is the thing `no-repair` removes.
"""

from __future__ import annotations

import pytest

from paginate import page_count


def test_a_partial_page_needs_another_page() -> None:
    assert page_count(11, 10) == 2


def test_a_full_page_of_items_needs_exactly_one_page() -> None:
    """The case ``total // per_page + 1`` gets wrong — it adds a page that would
    be empty."""
    assert page_count(10, 10) == 1


def test_no_items_needs_no_pages() -> None:
    assert page_count(0, 10) == 0


def test_two_full_pages() -> None:
    assert page_count(20, 10) == 2


def test_one_item_still_needs_a_page() -> None:
    assert page_count(1, 10) == 1


def test_rejects_a_nonpositive_page_size() -> None:
    with pytest.raises(ValueError):
        page_count(1, 0)
