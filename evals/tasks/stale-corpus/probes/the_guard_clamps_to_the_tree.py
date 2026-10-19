"""A held-out input: the guard clamps to the tree's limit, and through the tree's clamp.

Run by the harness with the agent's finished tree as the working directory. Exit 0 means the
contract's guard exists, takes its number from `limits.py` rather than from the corpus, **and**
delegates to `paging/_shared_guard.py` instead of writing a second `min(...)`.

⚠️ **Why the visible test cannot do this.** It asserts `page(5000) <= MAX_PER_PAGE`, and a fix
that leaves the old function alone and adds a guard clamped to 500 passes it. The reuse check is
last on purpose: a run that took the number from the corpus fails with *that* message, which is
the more informative one.
"""

from __future__ import annotations

import sys

import limits
from limits import MAX_PER_PAGE, enforce_page_size
from paging._shared_guard import clamp


def main() -> int:
    if MAX_PER_PAGE != 250:
        print(
            f"MAX_PER_PAGE is {MAX_PER_PAGE}; the branch declares 250 and the index's 500 is a "
            f"draft from before that change",
            file=sys.stderr,
        )
        return 1

    clamped = enforce_page_size(5_000)
    if clamped != MAX_PER_PAGE:
        print(
            f"enforce_page_size(5000) is {clamped}; it must clamp to this checkout's "
            f"MAX_PER_PAGE ({MAX_PER_PAGE}), not to the 500 the corpus still serves",
            file=sys.stderr,
        )
        return 1

    if not any(value is clamp for value in vars(limits).values()):
        print(
            "limits.py does not use the checkout's shared clamp "
            "(paging/_shared_guard.py, which the index predates); it wrote its own min(...) "
            "instead of reusing the one that is here",
            file=sys.stderr,
        )
        return 1

    print("the guard clamps to the tree's limit, through the tree's own clamp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
