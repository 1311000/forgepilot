"""Limits the listing applies when it renders.

⚠️ `MAX_PER_PAGE` here is a guess, and the ticket says so. The real value is published by the
reporting service, in a repository that is **not checked out** where this code lives — which is
the whole situation this task is about.
"""

from __future__ import annotations

#: How many rows one page of the listing holds.
MAX_PER_PAGE = 500
