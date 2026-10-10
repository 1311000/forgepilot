"""Paging arithmetic for the report listing.

The listing asks this module how many pages a result set needs, then renders that
many links. Getting it wrong by one is the difference between a page that exists
and a page nobody can reach.
"""

from __future__ import annotations


def page_count(total: int, per_page: int) -> int:
    """How many pages ``total`` items need when each page holds ``per_page``.

    ``11`` items at ``10`` per page is two pages: one full, one with a single
    item on it.
    """
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    return total // per_page
