"""What it takes for a mechanism to count as *priced*.

The matrix has always reported a pass rate per `(task, arm)` cell. A pass rate alone
cannot answer the question the ablation exists to answer — *does this mechanism earn its
place?* — because a cell can lose for a reason that has nothing to do with the mechanism:

- the arm lost because the agent left scratch files behind (tidiness, not the answer),
- the arm lost because its report sentence did not mention a word (narration, not the answer),
- the arm lost because a held-out probe caught it (a different oracle, not this task's answer).

So the rule is written down here, once, and applied to rows rather than to prose:

1. **Enough reps.** Fewer than `MIN_REPS` rows in either the arm or the full column means
   `insufficient`. Three is not statistics; it is the smallest number that can show a flip
   instead of a coin landing the same way twice.
2. **A real flip.** The arm must fail in at least two thirds of its reps *and* the full arm
   must pass in at least two thirds of its reps. Anything less is `unpriced` — the mechanism
   is reachable and busy but the task does not miss it.
3. **The flip must be the task's reason.** At least one failing arm row must carry a
   `behaviour` failure kind — the answer was wrong or absent (`checks`, `changed`, `state`,
   `delivery`, `other`), not merely untidy (`scratch`, `cost`), not merely quiet
   (`narration`), not a held-out probe. A flip without one is `priced_but_unclassified`:
   the arm did lose, and this file refuses to say why.
4. **Inversion is its own answer.** If the arm passes *more* often than the full arm, the
   verdict is `inverted`. That is not a weak mechanism; it is a task whose check is
   measuring something else, and the two must not be averaged into "no effect".

Rows written before the harness recorded `failure_kinds` still carry the failure text, and
the field is a pure function of that text, so the kinds are **re-derived** with the runner's own
classifier (`--no-derive` turns that off). A row with neither the field nor the text makes the
verdict `priced_but_unclassified`, which is the honest word for "the arm lost and the row does
not say why".
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "BEHAVIOUR_KINDS",
    "MIN_REPS",
    "VERDICTS",
    "Cell",
    "Pricing",
    "cells_from_rows",
    "summarise",
    "failure_kinds_of",
    "price",
]

#: The smallest number of reps in which a flip is visible at all.
MIN_REPS = 3

#: Failure kinds that mean *the answer was wrong or absent*. `scratch`/`cost` are tidiness,
#: `narration` is the report sentence, `probe` is a different oracle.
BEHAVIOUR_KINDS = frozenset({"checks", "changed", "state", "delivery", "other"})

VERDICTS = ("priced", "priced_but_unclassified", "unpriced", "inverted", "insufficient")


@dataclass(frozen=True)
class Cell:
    """One row of one `(task, arm)` cell, reduced to what pricing needs."""

    task: str
    arm: str
    rep: int
    passed: bool
    kinds: tuple[str, ...] = ()

    @property
    def behaviour(self) -> bool:
        """Whether this failure is about the answer rather than about tidiness or wording."""
        return any(kind in BEHAVIOUR_KINDS for kind in self.kinds)


@dataclass(frozen=True)
class Pricing:
    """The verdict for one `(task, arm)` pair, and the counts it was read from."""

    task: str
    arm: str
    reps: int
    arm_passed: int
    full_passed: int
    verdict: str
    reason: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "arm": self.arm,
            "reps": self.reps,
            "armPassed": self.arm_passed,
            "fullPassed": self.full_passed,
            "verdict": self.verdict,
            "reason": self.reason,
        }


def _two_thirds(count: int, total: int) -> bool:
    """Whether `count` out of `total` is at least two thirds — integer, no float drift."""
    return total > 0 and count * 3 >= total * 2


def failure_kinds_of(row: Mapping[str, Any], *, derive_missing: bool = True) -> tuple[str, ...]:
    """The row's failure kinds, deriving them from `failures` when the field is absent.

    Rows written before `failure_kinds` existed still carry the failure *text*, and the
    classifier that fills the field is a pure function of that text (`evals.harness.failure_kind`).
    Re-deriving is therefore the same answer the runner would have written, not a guess — and
    it is imported rather than reimplemented so the two cannot disagree. `derive_missing=False`
    is for a caller that wants to count "unclassified" separately.
    """
    raw = row.get("failure_kinds")
    if isinstance(raw, (list, tuple)) and raw:
        return tuple(str(kind) for kind in raw)
    failures = row.get("failures")
    if not derive_missing or not isinstance(failures, (list, tuple)):
        return ()
    # Imported here, not at module scope: `evals/pricing.py` is also a script, and when it is
    # run as one (`python evals/pricing.py`) the package name `evals` does not resolve — the
    # module is `harness` there. One classifier either way, which is the point.
    try:
        from evals.harness import failure_kind
    except ModuleNotFoundError:  # pragma: no cover - only when run as a script
        from harness import failure_kind

    return tuple(failure_kind(str(reason)) for reason in failures)


def cells_from_rows(
    rows: Iterable[Mapping[str, Any]], *, derive_missing_kinds: bool = True
) -> dict[str, dict[str, list[Cell]]]:
    """Group rows by task and then by arm, keeping every rep."""
    grouped: dict[str, dict[str, list[Cell]]] = {}
    for row in rows:
        task = str(row.get("task", ""))
        arm = str(row.get("arm", ""))
        if not task or not arm:
            continue
        cell = Cell(
            task=task,
            arm=arm,
            rep=int(row.get("rep", 0) or 0),
            passed=bool(row.get("passed")),
            kinds=failure_kinds_of(row, derive_missing=derive_missing_kinds),
        )
        grouped.setdefault(task, {}).setdefault(arm, []).append(cell)
    for arms in grouped.values():
        for name in arms:
            arms[name].sort(key=lambda cell: cell.rep)
    return grouped


def _priced(
    task: str, arm: str, arm_cells: Sequence[Cell], full_cells: Sequence[Cell], *, min_reps: int
) -> Pricing:
    reps = min(len(arm_cells), len(full_cells))
    arm_passed = sum(1 for cell in arm_cells if cell.passed)
    full_passed = sum(1 for cell in full_cells if cell.passed)
    arm_reps, full_reps = len(arm_cells), len(full_cells)

    if arm_reps < min_reps or full_reps < min_reps:
        return Pricing(
            task,
            arm,
            reps,
            arm_passed,
            full_passed,
            "insufficient",
            f"{arm_reps} rep(s) of {arm} and {full_reps} of full; "
            f"a flip needs {min_reps} of each to be visible at all",
        )
    if _two_thirds(arm_passed, arm_reps) and not _two_thirds(full_passed, full_reps):
        return Pricing(
            task,
            arm,
            reps,
            arm_passed,
            full_passed,
            "inverted",
            f"{arm} passed {arm_passed}/{arm_reps} while full passed {full_passed}/{full_reps}; "
            "the arm beats the whole, so the check measures something other than this mechanism",
        )
    if _two_thirds(arm_passed, arm_reps) or not _two_thirds(full_passed, full_reps):
        return Pricing(
            task,
            arm,
            reps,
            arm_passed,
            full_passed,
            "unpriced",
            f"{arm} passed {arm_passed}/{arm_reps} and full passed {full_passed}/{full_reps}; "
            "there is no flip to price",
        )
    failing = [cell for cell in arm_cells if not cell.passed]
    if any(cell.behaviour for cell in failing):
        kinds = sorted({kind for cell in failing for kind in cell.kinds if kind in BEHAVIOUR_KINDS})
        return Pricing(
            task,
            arm,
            reps,
            arm_passed,
            full_passed,
            "priced",
            f"{arm} failed {len(failing)}/{arm_reps} on {', '.join(kinds)} while full passed "
            f"{full_passed}/{full_reps}",
        )
    recorded = sorted({kind for cell in failing for kind in cell.kinds})
    detail = (
        f"the failures were {', '.join(recorded)}"
        if recorded
        else "the rows record no failure kind at all"
    )
    return Pricing(
        task,
        arm,
        reps,
        arm_passed,
        full_passed,
        "priced_but_unclassified",
        f"{arm} failed {len(failing)}/{arm_reps} while full passed {full_passed}/{full_reps}, "
        f"but {detail}: the arm lost without a behaviour failure saying why",
    )


def price(
    rows: Iterable[Mapping[str, Any]],
    *,
    arms: Sequence[str] | None = None,
    full: str = "full",
    min_reps: int = MIN_REPS,
    derive_missing_kinds: bool = True,
) -> tuple[Pricing, ...]:
    """Price every arm in `arms` (default: every arm any row mentions, minus `full`)."""
    grouped = cells_from_rows(rows, derive_missing_kinds=derive_missing_kinds)
    chosen = (
        list(arms)
        if arms
        else sorted({arm for arms in grouped.values() for arm in arms if arm != full})
    )
    verdicts: list[Pricing] = []
    for task in sorted(grouped):
        arms_here = grouped[task]
        full_cells = arms_here.get(full, [])
        for arm in chosen:
            if arm == full or arm not in arms_here:
                continue
            verdicts.append(_priced(task, arm, arms_here[arm], full_cells, min_reps=min_reps))
    return tuple(verdicts)


def summarise(verdicts: Iterable[Pricing]) -> dict[str, dict[str, int]]:
    """Count verdicts per task.

    The rule prices *arms*, not tasks. A task on which several arms that have nothing to do
    with the task all price is a task whose check is unstable, and this count is how a reader
    sees that without re-deriving it from the rows.
    """
    counts: dict[str, dict[str, int]] = {}
    for verdict in verdicts:
        bucket = counts.setdefault(verdict.task, {w: 0 for w in VERDICTS})
        bucket[verdict.verdict] = bucket.get(verdict.verdict, 0) + 1
    return counts


def _read(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals/pricing.py",
        description="Price each arm against the full arm, row by row, by the written rule.",
    )
    parser.add_argument("--results", required=True, type=Path, help="a live/scripted JSONL file")
    parser.add_argument("--arm", action="append", default=[], help="restrict to these arms")
    parser.add_argument("--full", default="full", help="the arm that has everything (default full)")
    parser.add_argument(
        "--min-reps", type=int, default=MIN_REPS, help=f"reps needed to price (default {MIN_REPS})"
    )
    parser.add_argument("--json", action="store_true", help="print the verdicts as JSONL")
    parser.add_argument(
        "--summary", action="store_true", help="print the per-task verdict counts instead"
    )
    parser.add_argument(
        "--no-derive",
        action="store_true",
        help="do not derive missing failure kinds from the failure text",
    )
    args = parser.parse_args(argv)

    try:
        rows = _read(args.results)
    except OSError as exc:
        print(f"cannot read {args.results}: {exc}", file=sys.stderr)
        return 2

    verdicts = price(
        rows,
        arms=args.arm or None,
        full=args.full,
        min_reps=args.min_reps,
        derive_missing_kinds=not args.no_derive,
    )
    if not verdicts:
        print(f"{args.results} has no arm to price against {args.full!r}", file=sys.stderr)
        return 2
    if args.json:
        for verdict in verdicts:
            print(json.dumps(verdict.as_payload(), ensure_ascii=False))
        return 0

    if args.summary:
        counts = summarise(verdicts)
        print(f"{'task':24} " + " ".join(f"{word:>12}" for word in VERDICTS))
        for task in sorted(counts):
            bucket = counts[task]
            print(f"{task:24} " + " ".join(f"{bucket.get(word, 0):>12}" for word in VERDICTS))
        return 0

    print(f"{'task':24} {'arm':16} {'arm':>6} {'full':>6}  {'verdict':24} reason")
    for verdict in verdicts:
        print(
            f"{verdict.task:24} {verdict.arm:16} "
            f"{verdict.arm_passed}/{verdict.reps:<4} {verdict.full_passed}/{verdict.reps:<4} "
            f" {verdict.verdict:24} {verdict.reason}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI is exercised through `main`
    raise SystemExit(main())
