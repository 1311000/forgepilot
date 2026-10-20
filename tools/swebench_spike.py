"""The SWE-bench spike's only entry point.

The dataset is not downloaded here on purpose: a benchmark that fetches its own
questions can also silently change them. Point `--instances` at a JSONL you have,
`--repos` at a directory holding one checkout per instance (`owner__name`), and
this refuses anything the spike could not measure honestly.

Exit codes: 0 every instance is usable, 1 at least one is not, 2 the dataset file
is missing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# `evals/` is a directory of scripts, not an installed package (the same choice
# `tools/check.py` makes): running this file puts `tools/` on sys.path, so the
# repository root has to be added for the adapter to be importable.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evals.swebench import MalformedInstance, load_instances, spike  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--instances", type=Path, required=True, help="instances.jsonl")
    parser.add_argument("--repos", type=Path, required=True, help="one checkout per instance")
    parser.add_argument("--limit", type=int, default=3, help="how many instances (default 3)")
    args = parser.parse_args(argv)

    try:
        instances = load_instances(args.instances, limit=args.limit)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except MalformedInstance as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not instances:
        print("error: the dataset holds no instances", file=sys.stderr)
        return 1

    rows = spike(instances, args.repos)
    width = max(len(row.instance_id) for row in rows)
    for row in rows:
        state = "ok  " if row.ok else "FAIL"
        print(f"{state} {row.instance_id:<{width}}  {row.reason or row.head}")

    bad = [row for row in rows if not row.ok]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} instance(s) ready to spend against a model")
    return 0 if not bad else 1


if __name__ == "__main__":  # pragma: no cover - the module is the test surface
    raise SystemExit(main())
