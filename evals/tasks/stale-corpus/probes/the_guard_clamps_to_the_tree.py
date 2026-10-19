"""A held-out input: the guard clamps to the limit *this checkout* declares.

Run by the harness with the agent's finished tree as the working directory. Exit 0 means the
contract's guard exists **and** takes its number from `limits.py` rather than from the corpus,
which still holds a draft that says 500.

⚠️ **Why the visible test cannot do this.** It asserts `page(5000) <= MAX_PER_PAGE`, and a fix
that leaves the old function alone and adds a guard clamped to 500 passes it. The defect is not
"there is no guard" — it is "the guard took its number from the wrong copy of the same file",
and only an input that goes through the guard can see it.
"""

from __future__ import annotations

import sys

from limits import MAX_PER_PAGE, enforce_page_size


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

    print("the guard clamps to the limit this checkout declares")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
