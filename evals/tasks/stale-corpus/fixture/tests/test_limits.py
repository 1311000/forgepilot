"""What the listing can check by itself.

⚠️ **Deliberately coarse, and the band is load-bearing.** It can tell that the page size is in a
range the listing can render; it cannot tell which value is right, because the value that is
right is the one *this checkout declares* and a test that named it would be a second copy of it.

The band is also the trap for a retriever that trusts the corpus: the index still holds a draft
of `limits.py` from before this branch changed the number, and 500 is outside the band. A run
that takes the limit from the corpus fails here; a run that takes it from the tree passes.
"""

from __future__ import annotations

from limits import MAX_PER_PAGE, page


def test_the_page_size_is_in_the_band_the_listing_can_render() -> None:
    assert 100 <= MAX_PER_PAGE <= 400


def test_a_page_is_never_larger_than_the_limit() -> None:
    """The shape of the fix, without naming the function the contract asks for."""
    assert page(5_000) <= MAX_PER_PAGE
