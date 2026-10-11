"""The contract, once it has been looked up.

⚠️ `250` is not a number this file invented. It is what
``git.local/demo/contracts:paging/contract.py@main`` publishes, and that repository is not
checked out where this code lives — reachable only through the retriever, which is the point of
the task.
"""

from __future__ import annotations

#: How many rows one page of the listing holds.
MAX_PER_PAGE = 250
