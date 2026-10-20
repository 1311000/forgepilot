"""How long one scripted task takes, end to end, as a distribution rather than a number.

    python tools/bench.py                              # five iterations, prints p50/p95
    python tools/bench.py --iterations 9 --max-p95-ms 20000

**What this measures, and what it deliberately does not.** A `--scripted` run swaps the
model for the replay provider, so the number is the harness: boot, config resolution, a
session, a plan, the tool pipeline, the checks, the commit. That is the part this repository
owns and the part whose regression is worth catching. A live model's latency is the
provider's, moves with the network, and measuring it here would put a number nobody can act
on beside one they can — the eval matrix is where model behaviour belongs.

**Why p95 and not a mean.** A mean over five runs hides the tail, and the tail is what a
person waits on. With `--iterations 5` the p95 is effectively the worst run: that is the
intended reading at this sample size, not a statistical claim.

**Why it is not in `tools/check.py`.** A wall-clock gate on a shared CI runner fails for
load, for a slow filesystem, for a cold cache — none of which is a commit's fault — and a
gate that fails for reasons nobody caused is a gate that gets retried until it passes. It
runs on the weekly job at a deliberately loose bound (the point is "the harness did not
become pathological"), and the exact numbers are printed for whoever reads the log.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

FORGE = Path(__file__).resolve().parents[1]
RESULTS = FORGE / "evals" / "results"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    # Nearest-rank: ceil(fraction * n) - 1. `round` would report the median as the p95
    # for small samples, which is the one error this function must not make.
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _newest_result() -> Path | None:
    files = sorted(RESULTS.glob("*-scripted.jsonl"))
    return files[-1] if files else None


def _turn_seconds(path: Path | None) -> float | None:
    """The row's own in-process time, or `None` when the file cannot be read."""
    if path is None:
        return None
    try:
        line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
        return float(json.loads(line).get("wall_s"))
    except (OSError, ValueError, IndexError, TypeError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--iterations", type=int, default=5, metavar="N")
    parser.add_argument("--task", default="bug-fix")
    parser.add_argument("--arm", default="full")
    parser.add_argument(
        "--max-p95-ms",
        type=float,
        default=None,
        metavar="MILLISECONDS",
        help="exit non-zero when the p95 of boot+turn exceeds this",
    )
    args = parser.parse_args(argv)

    end_to_end: list[float] = []
    turns: list[float] = []
    for iteration in range(1, max(1, args.iterations) + 1):
        before = time.monotonic()
        completed = subprocess.run(
            [
                sys.executable,
                str(FORGE / "evals" / "run.py"),
                "--scripted",
                "--task",
                args.task,
                "--arm",
                args.arm,
                "--reps",
                "1",
            ],
            cwd=FORGE,
            capture_output=True,
            text=True,
        )
        elapsed = time.monotonic() - before
        if completed.returncode != 0:
            print(
                f"iteration {iteration}: the scripted run failed "
                f"({completed.returncode}): {completed.stderr.strip()[-800:]}",
                file=sys.stderr,
            )
            return 2
        end_to_end.append(elapsed)
        turn = _turn_seconds(_newest_result())
        if turn is not None:
            turns.append(turn)
        print(
            f"[{iteration}/{args.iterations}] boot+turn {elapsed:5.2f}s"
            + (f"  turn {turn:5.2f}s" if turn is not None else "")
        )

    p50 = _percentile(end_to_end, 0.50)
    p95 = _percentile(end_to_end, 0.95)
    print(
        f"\n{args.task}/{args.arm}: boot+turn p50 {p50:.2f}s  p95 {p95:.2f}s  (n={len(end_to_end)})"
    )
    if turns:
        print(
            f"harness turn:     p50 {_percentile(turns, 0.50):.2f}s  "
            f"p95 {_percentile(turns, 0.95):.2f}s  (n={len(turns)})"
        )
    if args.max_p95_ms is not None:
        bound = args.max_p95_ms / 1000.0
        if p95 > bound:
            print(
                f"FAILED: p95 {p95:.2f}s exceeds {bound:.2f}s. The bound is loose on purpose; "
                f"a run this far over means the harness got pathologically slower, not that "
                f"the machine was busy.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
