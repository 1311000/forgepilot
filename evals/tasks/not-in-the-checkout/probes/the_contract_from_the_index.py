"""A held-out input: the *exact* contract, which only the index carries.

Run by the harness with the agent's finished tree as the working directory. Exit 0 means the
listing pages at the value the reporting service publishes; a non-zero exit is what a run that
picked a plausible number inside the visible band produces.

⚠️ **Why the visible test cannot do this.** It asserts a band, because anything sharper would
have to name the value — and a value named in a file the agent can read is a value the agent can
copy without ever consulting the index. That is the difference between this task measuring
retrieval and measuring arithmetic.

⚠️ **And why `no-search` cannot pass it either way.** The contract lives in
`git.local/demo/contracts`, which is not in the checkout, so `findstr /s` and `grep -r` have
nothing to find: this is the first task in the set where removing the retriever is a
*structural* failure rather than an expensive one.
"""

from __future__ import annotations

import sys

from limits import MAX_PER_PAGE


def main() -> int:
    if MAX_PER_PAGE != 250:
        print(
            f"MAX_PER_PAGE is {MAX_PER_PAGE}; the reporting service publishes 250 "
            f"(git.local/demo/contracts:paging/contract.py@main)",
            file=sys.stderr,
        )
        return 1

    print("the listing pages at the published contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
