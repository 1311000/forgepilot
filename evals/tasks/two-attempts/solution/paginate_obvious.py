"""⚠️ The fix the ticket's example invites, and it is wrong.

``total // per_page + 1`` turns ``page_count(11, 10)`` from 1 into 2, which is
exactly what the ticket reports — and it turns ``page_count(10, 10)`` into 2 and
``page_count(0, 10)`` into 1, which is not. An agent that reads the one example and
writes this never learns it was wrong unless something runs the suite and tells it.

This file is a **script input**, never a shipped solution: it exists so the offline
run has a first attempt to fail with.
"""

from __future__ import annotations


def page_count(total: int, per_page: int) -> int:
    """How many pages ``total`` items need when each page holds ``per_page``.

    ``11`` items at ``10`` per page is two pages: one full, one with a single
    item on it.
    """
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    return total // per_page + 1
