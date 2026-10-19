"""The correct fix: the guard the contract names, built on the checkout's shared clamp.

`MAX_PER_PAGE` is already 250 here — the interesting part is that it stays 250, and that the
clamp comes from `paging/_shared_guard.py`, which the index does not have. An agent reading only
the corpus writes a second `min(...)` instead, and the probe can see the difference.
"""

from __future__ import annotations

from paging._shared_guard import clamp

MAX_PER_PAGE = 250


def enforce_page_size(requested: int) -> int:
    """The name the paging contract requires; the limit stays the one this file declares."""
    return clamp(requested, MAX_PER_PAGE)


def page(requested: int) -> int:
    """Kept for the callers that spell it this way."""
    return enforce_page_size(requested)
