"""Run the whole offline matrix and check it against the verdicts this repo recorded.

    python evals/matrix.py              # run all 8 arms x 11 tasks, then verify
    python evals/matrix.py --check FILE # verify an existing results file

**Why a matrix and not a number.** `--scripted` measures nothing about the agent: the
replay model is deterministic and reads no files, so the arms that switch a mechanism
off cannot move its answer. What the offline matrix *can* say is that the harness still
tells its own shapes apart — that `two-attempts` still needs the repair loop, that the
remaining nine tasks still finish in every arm, that each arm still switches exactly
what it claims to. That is a property of this tree, it costs nothing to run, and it is
the only kind of eval a schedule can afford.

**Why the expectations are written down rather than computed.** A check that derives
its verdict from the run it is checking cannot fail. The table below was measured on
2026-10-18 (`evals/results/20261018T083206-scripted.jsonl`, 78/80 over ten tasks) and
`stale-corpus` joined it on 2026-10-19 as the eleventh, offline-green in every arm — its
discriminating column is live, where a provider that serves a stale corpus copy is what
`--scripted` cannot see. A row that moves is a change in the harness and has to be
*decided*, not absorbed.

**What it does not check.** Whether any mechanism earns its place — that needs `--live`
and money, and `evals/report.py` answers it. A green matrix here must never be quoted
as a capability number.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness as H  # noqa: E402

RESULTS = H.HERE / "results"

#: The cells that are **expected to fail** offline, and only these.
#:
#: `two-attempts` seeds a bug whose first fix attempt leaves the check red, so the arm
#: that removes the repair loop (`no-repair`) and the arm that removes everything but
#: the loop (`baseline`) cannot pass it. Every other task is a shape the loop reaches
#: in one attempt, so a failure there is a finding.
EXPECTED_FAILURES: dict[str, frozenset[str]] = {
    "baseline": frozenset({"two-attempts"}),
    "full": frozenset(),
    "no-distill": frozenset(),
    "no-memory": frozenset(),
    "no-pinning": frozenset(),
    "no-repair": frozenset({"two-attempts"}),
    "no-search": frozenset(),
    "tight-window": frozenset(),
}

#: Arms that should switch nothing off, so a row from them claiming changes is a bug in
#: the arm — the table this harness spent most of its life confusing with the result.
UNCHANGED_ARMS = frozenset({"full"})


def expected(arm: str, task: str) -> bool:
    """Whether `(task, arm)` is supposed to pass offline."""
    return task not in EXPECTED_FAILURES[arm]


def check(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Every way the rows disagree with the recorded table, as printable lines."""
    problems: list[str] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    expected_cells = {(task, name) for name in EXPECTED_FAILURES for task in H.available_tasks()}

    for row in rows:
        key = (str(row.get("task")), str(row.get("arm")))
        if row.get("scratch_inside_repo"):
            # The row's `passed` is not evidence of anything when the shell could walk to
            # `evals/tasks/<name>/solution/`. It stays in the file (a sweep that happened is
            # a fact) and it stops being a cell of the table.
            problems.append(
                f"{key[0]}/{key[1]}: scored with the task's own answer within reach of the "
                "shell (the scratch root was inside the checkout), so it is not a measurement"
            )
            seen[key] = row
            continue
        if key in seen:
            problems.append(f"{key[0]}/{key[1]}: the same cell appears twice")
            continue
        seen[key] = row
        if key not in expected_cells:
            problems.append(f"{key[0]}/{key[1]}: a cell the table does not name")
            continue
        want = expected(key[1], key[0])
        got = bool(row.get("passed"))
        if got != want:
            problems.append(
                f"{key[0]}/{key[1]}: expected {'pass' if want else 'fail'}, "
                f"got {'pass' if got else 'fail'}"
                + (f" ({row['failures']})" if row.get("failures") else "")
            )
        if key[1] in UNCHANGED_ARMS and row.get("arm_changes"):
            problems.append(f"{key[0]}/{key[1]}: switched {row['arm_changes']!r}, not nothing")

    for key in sorted(expected_cells - set(seen)):
        problems.append(f"{key[0]}/{key[1]}: no row at all")
    return problems


def newest_results() -> Path:
    files = sorted(RESULTS.glob("*-scripted.jsonl"))
    if not files:
        raise FileNotFoundError(f"no scripted results in {RESULTS}; run without --check")
    return files[-1]


def read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def run_matrix() -> Path:
    """Run every arm over every task, once each, and return the results file."""
    H.refuse_scratch_inside_the_repository()
    command = [sys.executable, str(H.HERE / "run.py"), "--scripted", "--reps", "1"]
    for name in EXPECTED_FAILURES:
        command += ["--arm", name]
    print("+ " + " ".join(command))
    subprocess.run(command, check=True)
    return newest_results()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        metavar="RESULTS.jsonl",
        default=None,
        help="verify an existing results file instead of running the matrix",
    )
    args = parser.parse_args(argv)

    if args.check:
        path = Path(args.check)
        if not path.is_file():
            path = RESULTS / path.name
        if not path.is_file():
            print(f"error: no such results file: {args.check}", file=sys.stderr)
            return 2
    else:
        path = run_matrix()

    rows = read_rows(path)
    problems = check(rows)
    cells = len({(r.get("task"), r.get("arm")) for r in rows})
    print(f"\n{path.name}: {cells} cell(s), {sum(1 for r in rows if r.get('passed'))} passed")
    if problems:
        for problem in problems:
            print(f"  MISMATCH  {problem}")
        print(f"\n{len(problems)} cell(s) disagree with the recorded matrix")
        return 1
    print("every cell matches the recorded matrix")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
