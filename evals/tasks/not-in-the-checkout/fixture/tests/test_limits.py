"""What the listing can check by itself.

⚠️ **This test is deliberately coarse, and that is the design rather than laziness.** It can
tell that the value is in a sane band; it cannot tell which value is *right*, because the right
one is published in another repository. Anything a local test could assert here would also be
readable by the agent, and an answer that is readable is an answer that can be guessed instead
of looked up — which would make this task measure arithmetic instead of retrieval.

The exact contract is checked by `probes/`, which the agent never sees.
"""

from __future__ import annotations

from limits import MAX_PER_PAGE


def test_the_page_size_is_in_the_band_the_listing_can_render() -> None:
    """A page has to be big enough to be worth a request and small enough to render."""
    assert 100 <= MAX_PER_PAGE <= 400
