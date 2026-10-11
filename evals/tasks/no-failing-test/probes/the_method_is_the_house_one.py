"""A held-out input: every case here is one where the two methods disagree.

Run by the harness with the agent's finished tree as the working directory. Exit 0
means the **house method** is in the module; a non-zero exit is what a run that left
the interpolation alone produces — including the run that documented it, and
including the run that "unified" the two modules by changing the report instead.

⚠️ **Why this task needs a probe at all.** Every other task in this set has
something that fails: a check, a missing feature, a permission refusal. This one has
nothing. The visible suite is green before the fix and green after it, so the only
instrument that can tell the two trees apart is an input the agent cannot read while
it works. Without this file the task would score 100% for every implementation,
including both wrong ones.
"""

from __future__ import annotations

import sys

from percentiles import p50, p95, percentile
from reporting import percentile_rank

#: ``(values, p, the nearest-rank answer)``. The parenthesised number is what the
#: interpolating implementation returns — carried here because the gap is the whole
#: content of the case, and a reader checking this file by hand should not have to
#: recompute it.
CASES = [
    ([1.0, 2.0, 3.0, 4.0], 50, 2.0),  # interpolation: 2.5
    ([1.0, 2.0, 3.0, 4.0], 25, 1.0),  # interpolation: 1.75
    ([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 50, 3.0),  # interpolation: 3.5
    ([float(n) for n in range(1, 11)], 90, 9.0),  # interpolation: 9.1
    ([10.0, 20.0, 30.0, 40.0], 95, 40.0),  # interpolation: 38.5
]


def main() -> int:
    for values, p, expected in CASES:
        got = percentile(values, p)
        if abs(got - expected) > 1e-9:
            print(
                f"percentile({values}, {p}) -> {got}, the house method gives {expected}",
                file=sys.stderr,
            )
            return 1

    # The two panels the ticket names, through the functions they actually call —
    # a `percentile` that is right while `p95` still interpolates would pass the loop
    # above and leave the reported symptom in place.
    if abs(p50([1.0, 2.0, 3.0, 4.0]) - 2.0) > 1e-9:
        print("p50 of an even-length series is still interpolating", file=sys.stderr)
        return 1
    if abs(p95([10.0, 20.0, 30.0, 40.0]) - 40.0) > 1e-9:
        print("p95 of an even-length series is still interpolating", file=sys.stderr)
        return 1

    # The ticket's actual complaint — "the two reports disagree" — checked directly,
    # against whichever `reporting.py` the run left behind. The hardcoded answers
    # above are what make this safe: agreeing on a *wrong* method fails them.
    for values, p, _ in CASES:
        if abs(percentile(values, p) - percentile_rank(values, p)) > 1e-9:
            print(
                f"the dashboard and the report still disagree at p={p} on {values}",
                file=sys.stderr,
            )
            return 1

    print("held-out inputs agree, and both panels now use one method")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
