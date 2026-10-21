"""The recorded runs, and the two numbers P14 had to correct.

A score nobody can re-read is a claim. This file keeps `evals/swebench_runs.jsonl`
honest: every row names a real verdict, and a `resolved` row cannot carry a failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

FORGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE / "tools"))
sys.path.insert(0, str(FORGE))

import swebench_grade as G  # noqa: E402

from evals.swebench import SwebenchInstance, load_instances  # noqa: E402

RECORD = FORGE / "evals" / "swebench_runs.jsonl"
FIXTURE = FORGE / ".tmp" / "swebench_lite.jsonl"
VERDICTS = {"resolved", "unresolved", "unknown", "not_run"}


def rows() -> list[dict]:
    return [json.loads(line) for line in RECORD.read_text(encoding="utf-8").splitlines() if line]


def test_every_recorded_row_is_a_verdict_this_project_uses() -> None:
    recorded = rows()
    assert recorded, "an empty record is not a record"
    for row in recorded:
        assert row["verdict"] in VERDICTS, row
        assert row["f2p_failed"] >= 0 and row["p2p_failed"] >= 0, row
        if row["verdict"] == "resolved":
            assert row["f2p_failed"] == 0 and row["p2p_failed"] == 0, row
        else:
            assert row["f2p_failed"] or row["p2p_failed"], row


def test_no_instance_is_recorded_twice() -> None:
    ids = [row["instance"] for row in rows()]
    assert len(ids) == len(set(ids)), ids


def test_a_recorded_row_names_tests_the_instance_actually_has() -> None:
    if not FIXTURE.is_file():
        pytest.skip("the instance file is not on this machine")
    known = {item.instance_id: item for item in load_instances(FIXTURE, limit=100)}
    for row in rows():
        instance = known.get(row["instance"])
        if instance is None:
            continue
        assert row["fail_to_pass"] == len(instance.fail_to_pass), row
        assert row["pass_to_pass"] == len(instance.pass_to_pass), row


def test_the_recipe_retries_a_mirror_that_is_down_and_fails_loudly() -> None:
    from swebench_env import APT

    assert "for i in 1 2 3" in APT
    assert "apt could not install patch" in APT


def test_the_files_behind_the_lists_are_read_from_both() -> None:
    instance = SwebenchInstance(
        instance_id="x",
        repo="a/b",
        base_commit="c",
        problem_statement="d",
        fail_to_pass=("t/fix.py::test_fix",),
        pass_to_pass=("t/keep.py::test_keep", "t/fix.py::test_neighbour"),
    )
    assert G.all_test_files(instance) == ("t/fix.py", "t/keep.py")
