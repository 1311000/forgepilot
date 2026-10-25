"""The scale bench: deterministic corpus, the same percentile rule, and a row of numbers.

Nothing here asserts a *latency* — a test that fails when the machine is busy teaches people to
skip it. What is asserted is that the tree is reproducible, that the counts are the ones the
corpus will see, and that the percentile rule is the one `tools/load_single_replica.py` uses.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_tool():
    path = ROOT / "tools" / "bench_scale.py"
    spec = importlib.util.spec_from_file_location("bench_scale_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = load_tool()


def test_the_tree_is_reproducible(tmp_path):
    first = tool.generate(tmp_path / "a", files=120, repos=3, seed=7)
    second = tool.generate(tmp_path / "b", files=120, repos=3, seed=7)
    assert first == second
    for slug, path in first[::17]:
        left = (tmp_path / "a" / slug.replace("/", "_") / path).read_text(encoding="utf-8")
        right = (tmp_path / "b" / slug.replace("/", "_") / path).read_text(encoding="utf-8")
        assert left == right


def test_a_different_seed_is_a_different_tree(tmp_path):
    first = tool.generate(tmp_path / "a", files=60, repos=2, seed=1)
    second = tool.generate(tmp_path / "b", files=60, repos=2, seed=2)
    left = (tmp_path / "a" / first[0][0].replace("/", "_") / first[0][1]).read_text(
        encoding="utf-8"
    )
    right = (tmp_path / "b" / second[0][0].replace("/", "_") / second[0][1]).read_text(
        encoding="utf-8"
    )
    assert left != right


def test_the_needle_exists_and_is_rare(tmp_path):
    written = tool.generate(tmp_path / "c", files=400, repos=2, seed=3)
    with_needle = 0
    for slug, path in written:
        text = (tmp_path / "c" / slug.replace("/", "_") / path).read_text(encoding="utf-8")
        if "NEEDLE = " in text:
            with_needle += 1
    assert with_needle == 4  # one every 100 files


def test_the_corpus_counts_what_the_generator_wrote(tmp_path):
    written = tool.generate(tmp_path / "d", files=40, repos=2, seed=5)
    files, symbols = tool.corpus_of(tmp_path / "d", written, ref="<worktree>")
    assert len(files) == 40
    assert len(symbols) == 40 * tool.FUNCTIONS_PER_FILE
    assert all(item.ref == "<worktree>" for item in files)


def test_summarise_is_the_nearest_rank_rule():
    row = tool.summarise([1.0, 2.0, 3.0, 4.0])
    assert row["p50"] == 2.0
    assert row["p95"] == 4.0
    assert row["n"] == 4.0


def test_the_cli_prints_one_json_row(tmp_path, capsys):
    code = tool.main(
        [
            "--files",
            "40",
            "--repos",
            "2",
            "--repeats",
            "5",
            "--delta",
            "5",
            "--skip-http",
            "--json",
            "--out",
            str(tmp_path / "run"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["files"] == 40
    assert payload["symbols"] == 160
    assert set(payload["searchMs"]) == {"needle", "def", "glob"}
    assert payload["buildSeconds"] >= 0
