"""⚠️ The plausible wrong answer: a number inside the visible band, picked instead of looked up.

`200` passes the listing's own test — it is big enough to be worth a request and small enough to
render — and it is not the contract. This is what a run does when it cannot reach the index, or
does not try: the suite goes green and the pages still do not line up with the report that quotes
them.

**Script input, never shipped code.**
"""

from __future__ import annotations

#: How many rows one page of the listing holds. (A guess, like the one the ticket is about.)
MAX_PER_PAGE = 200
