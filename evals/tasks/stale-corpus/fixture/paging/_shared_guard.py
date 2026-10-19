"""The one place paging code clamps a caller's requested size.

⚠️ **This file is newer than the corpus.** The index this task points at was built before it
existed, so a retriever that answers only from the corpus cannot find it: `code_search` returns
nothing for `clamp`, and an agent on that retriever has to invent its own. The ticket says to
reuse this one, which is the only fact in the task that is *not* in the index.
"""

from __future__ import annotations


def clamp(requested: int, limit: int) -> int:
    """The listing's page size, whatever the caller asked for."""
    return min(requested, limit)
