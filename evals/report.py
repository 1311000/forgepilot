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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--latest", action="store_true", help="only the newest result file")
    parser.add_argument("--file", default=None, help="one result file by name")
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
    _print_arms(rows)

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
