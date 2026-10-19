"""The correct fix: the guard the contract names, clamped to the limit the *tree* declares.

`MAX_PER_PAGE` is already 250 here — the interesting part is that it stays 250. The index still
holds a draft of this file that says 500, and a run that read its limit from the corpus writes
500 here and fails the visible band.
"""

from __future__ import annotations

MAX_PER_PAGE = 250


def enforce_page_size(requested: int) -> int:
    """The name the paging contract requires; the limit stays the one this file declares."""
    return min(requested, MAX_PER_PAGE)


def page(requested: int) -> int:
    """Kept for the callers that spell it this way."""
    return enforce_page_size(requested)
