"""Paging limits for the listing.

⚠️ **This file is the authority for the number**, and the contract on purpose does not restate
it: two copies of a limit are two limits, and the one that wins is whichever file the agent read
last. The contract names the *guard* instead — the part that cannot be derived from anything in
this checkout.
"""

from __future__ import annotations

#: How many rows one page of the listing holds.
MAX_PER_PAGE = 250


def page(requested: int) -> int:
    """The listing's own page size, before the contract's guard exists.

    It trusts the caller, which is the bug the ticket describes: a request for 5 000 rows is
    answered with 5 000 rows.
    """
    return requested
