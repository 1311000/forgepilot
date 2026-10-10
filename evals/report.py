"""Summarise eval results.

    python evals/report.py                     # every result file
    python evals/report.py --latest            # just the newest run

Reads the JSONL the runner appends and prints two tables: per (task, arm), and
per arm. **Nothing here is a verdict on its own.** A single row says almost
nothing about a stochastic system, and the report says so where it prints rather
than leaving the reader to remember it.

⚠️ **Scripted rows measure the harness, not the agent.** A replay model performs
a fix because it was told to; the rate it produces is a statement about the
machine. They are included because a broken harness must be visible, and labelled
so nobody reads them as a delivery rate.
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parent / "results"


def load(pattern: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(glob.glob(str(RESULTS / pattern))):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _median(values: list[float]) -> str:
    return f"{statistics.median(values):.0f}" if values else "-"


def _print_grouped(rows: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["task"], row["arm"])].append(row)

    print(f"{'task':<14}{'arm':<14}{'mode':<10}{'n':>3}{'pass':>7}{'calls':>7}{'turns':>7}{'secs':>7}  states")
    print("-" * 88)
    for (task, arm), group in sorted(groups.items()):
        passed = sum(1 for r in group if r.get("passed"))
        states = ",".join(sorted({str(r.get("state", "-")) for r in group}))
        print(
            f"{task:<14}{arm:<14}{group[0].get('mode', '-'):<10}"
            f"{len(group):>3}{passed:>4}/{len(group):<2}"
            f"{_median([r.get('tool_calls', 0) for r in group]):>7}"
            f"{_median([r.get('turns', 0) for r in group]):>7}"
            f"{_median([r.get('wall_s', 0) for r in group]):>7}  {states}"
        )


def _print_arms(rows: list[dict[str, Any]]) -> None:
    """The comparison the arms exist for: the same tasks, one mechanism off."""
    arms: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        arms[row["arm"]].append(row)
    if len(arms) < 2:
        return

    live = [r for r in rows if r.get("mode") == "live"]
    if not live:
        print(
            "\nOnly scripted rows so far — an arm table over them would compare the "
            "harness with itself. Run with `--live`."
        )
        return

    print(f"\n{'arm':<16}{'n':>4}{'pass rate':>12}{'median calls':>14}{'median secs':>13}")
    print("-" * 60)
    base = arms.get("full", [])
    for arm, group in sorted(arms.items(), key=lambda kv: (kv[0] != "full", kv[0])):
        passed = sum(1 for r in group if r.get("passed"))
        rate = passed / len(group)
        delta = ""
        if base and arm != "full":
            base_rate = sum(1 for r in base if r.get("passed")) / len(base)
            delta = f"  ({rate - base_rate:+.0%} vs full)"
        print(
            f"{arm:<16}{len(group):>4}{rate:>11.0%}{_median([r.get('tool_calls', 0) for r in group]):>14}"
            f"{_median([r.get('wall_s', 0) for r in group]):>13}{delta}"
        )


def _print_matrix(rows: list[dict[str, Any]]) -> None:
    """Which **cell** moved. A rate per arm hides the one that flipped.

    An arm's overall rate is an average over tasks that may disagree completely:
    `no-repair` at 100% on four of them and 0% on the fifth and 100% overall is not
    a contradiction, it is two different statements. The matrix is what makes the
    second one visible, and it is where an ablation is actually read.
    """
    tasks = sorted({r["task"] for r in rows})
    arms = sorted({r["arm"] for r in rows}, key=lambda a: (a != "full", a))
    # Two arms is the requirement — one arm has nothing to compare against and the
    # grouped table above already says it. One *task* is fine: re-running a single
    # cell under two arms is how a suspicion gets checked, and that is exactly when
    # the marking matters.
    if len(arms) < 2:
        return

    counts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        counts[(row["task"], row["arm"])].append(row)

    width = max(len(a) for a in arms) + 2
    print(f"\n{'task':<16}" + "".join(f"{a:>{width}}" for a in arms))
    print("-" * (16 + width * len(arms)))
    for task in tasks:
        baseline = counts.get((task, "full"), [])
        base_pass = sum(1 for r in baseline if r.get("passed"))
        cells = []
        for arm in arms:
            group = counts.get((task, arm), [])
            if not group:
                cells.append(f"{'-':>{width}}")
                continue
            passed = sum(1 for r in group if r.get("passed"))
            mark = ""
            if arm != "full" and baseline and passed < base_pass:
                # ⚠️ The cell the ablation exists to find. `*` rather than a colour
                # because this has to survive being pasted into a message.
                mark = "*"
            cells.append(f"{f'{passed}/{len(group)}{mark}':>{width}}")
        print(f"{task:<16}" + "".join(cells))

    if any(
        arm != "full" and len(counts.get((task, arm), []))
        for task in tasks
        for arm in arms
    ):
        print("\n* = fewer passes than `full` on that task — the cell to read")


def _print_failures(rows: list[dict[str, Any]]) -> None:
    """**Why** the runs that did not pass, did not pass.

    The rate is the least interesting number in this file. `score()` returns
    reasons precisely so this question is answerable — *"how often, without in what
    way, is not actionable"* — and until this existed the report printed the rate
    and dropped every reason on the floor, while `run.py` printed them inline and
    they scrolled away.

    It matters most for an ablation, where the interesting output is almost never
    "the arm scored lower" but "the arm scored lower, and here is the sentence
    saying why" — which is how the two defects in this harness were actually found.
    Both times, the reason was read rather than the number.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row.get("passed"):
            groups[(row["task"], row["arm"])].append(row)

    if not groups:
        print("\nNo run failed, so nothing here says what a failure looks like.")
        return

    print("\nWhy the runs that did not pass, did not pass")
    print("-" * 88)
    for (task, arm), group in sorted(groups.items()):
        reasons: dict[str, int] = defaultdict(int)
        for row in group:
            for reason in row.get("failures") or ("(no reason recorded)",):
                reasons[_shorten(str(reason))] += 1
        print(f"{task} / {arm}  — {len(group)} of the rows above")
        for reason, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {count}x  {reason}")
        print()

    print(
        "A reason repeated on every row of a cell is the cell's behaviour; one that "
        "appears once is one run's."
    )


def _print_agent_text(rows: list[dict[str, Any]], limit: int = 400) -> None:
    """What the agent *said* in the runs that did not pass. Opt-in.

    ⚠️ **This is the section that has actually found things.** Twice now the reason
    a run failed was not in `failures` at all: once it was an agent that had
    diagnosed the bug correctly and declined to fix it, and once it was the same
    agent quoting `find-the-bug-no-repair-1` — its own working directory — as if the
    treatment were part of the ticket. Neither is visible in a rate, and the second
    one is not visible in a failure *reason* either.

    Collapsed to one bounded excerpt per row, because the whole point is to read it.
    """
    failed = [r for r in rows if not r.get("passed") and r.get("agent_text")]
    if not failed:
        return

    print("\nWhat the agent said in those runs  (`--agent-text`)")
    print("-" * 88)
    for row in failed:
        print(f"{row['task']} / {row['arm']} / rep{row.get('rep')}  ({row.get('state')})")
        print(f"    …{_shorten(str(row['agent_text'])[-limit:], limit)}")
        print()


def _shorten(text: str, limit: int = 150) -> str:
    """One line, bounded — a traceback in the middle of a table helps nobody."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--latest", action="store_true", help="only the newest result file")
    parser.add_argument("--file", default=None, help="one result file by name")
    parser.add_argument(
        "--agent-text",
        action="store_true",
        help="also print what the agent said in the runs that did not pass",
    )
    args = parser.parse_args(argv)

    if args.file:
        rows = load(args.file)
    elif args.latest:
        files = sorted(RESULTS.glob("*.jsonl"))
        if not files:
            print(f"no results in {RESULTS}. Run `evals/run.py` first.", file=sys.stderr)
            return 2
        rows = load(files[-1].name)
    else:
        rows = load("*.jsonl")

    if not rows:
        print(f"no results in {RESULTS}. Run `evals/run.py` first.", file=sys.stderr)
        return 2

    modes = ",".join(sorted({str(r.get("mode")) for r in rows}))
    print(f"{len(rows)} row(s) from {len({r.get('run') for r in rows})} run(s) [{modes}]\n")
    _print_grouped(rows)
    _print_matrix(rows)
    _print_arms(rows)
    _print_failures(rows)
    if args.agent_text:
        _print_agent_text(rows)

    print(
        "\nA rate over a handful of runs is a hint, not a measurement: the same task "
        "against the same model varies run to run. Use --reps."
    )
    if "scripted" in modes:
        print(
            "Scripted rows measure the harness. A replay model performs a fix because "
            "it was told to, so those rates say nothing about an agent."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
