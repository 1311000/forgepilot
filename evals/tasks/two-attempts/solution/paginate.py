"""The fix that survives the whole suite, not just the reported example.

Ceiling division, with ``0`` handled before it rather than by the arithmetic:
``(0 + 10 - 1) // 10`` is ``0`` already, but relying on that makes the empty case
read like an accident. The ``total <= 0`` branch also documents that a negative
total is zero pages rather than a negative count.
"""

from __future__ import annotations


def page_count(total: int, per_page: int) -> int:
    """How many pages ``total`` items need when each page holds ``per_page``.

    ``11`` items at ``10`` per page is two pages: one full, one with a single
    item on it.
    """
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    if total <= 0:
        return 0
    return (total + per_page - 1) // per_page
