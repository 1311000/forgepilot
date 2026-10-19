"""The benchmark's arithmetic, because the p95 index is easy to get silently wrong.

`tools/bench.py` is a script, not a package, so it is loaded by path. The two pieces worth
pinning are the percentile (an off-by-one there reports the median as the tail, and a
"p95" that is really the middle is the sort of number that looks fine forever) and the
reader of a results file (a missing or half-written file must degrade to "no number"
rather than raise, because a benchmark that crashes tells nobody anything about latency).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

FORGE = Path(__file__).resolve().parents[2]


def _bench():
    spec = importlib.util.spec_from_file_location("forgepilot_bench", FORGE / "tools" / "bench.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_p95_with_five_samples_is_the_worst_of_them() -> None:
    bench = _bench()
    values = [1.0, 2.0, 3.0, 4.0, 9.0]
    assert bench._percentile(values, 0.95) == 9.0
    assert bench._percentile(values, 0.50) == 3.0


def test_percentile_of_nothing_is_zero_rather_than_a_crash() -> None:
    bench = _bench()
    assert bench._percentile([], 0.95) == 0.0


def test_a_results_file_that_cannot_be_read_gives_no_number(tmp_path: Path) -> None:
    bench = _bench()
    assert bench._turn_seconds(None) is None
    broken = tmp_path / "broken.jsonl"
    broken.write_text("this is not json\n", encoding="utf-8")
    assert bench._turn_seconds(broken) is None
    good = tmp_path / "good.jsonl"
    good.write_text(json.dumps({"wall_s": 1.5}) + "\n", encoding="utf-8")
    assert bench._turn_seconds(good) == 1.5
