"""A variant may replace a plugin row, and `stale-corpus` is why.

The composite retriever's justification is a comparison — the same ticket, the same script, one
row different (`index-only`). A variant could change `script` and `expect` and nothing else, so
that comparison had no way to be expressed; it needs `patches`.

⚠️ **Replaced, not appended.** A variant's `patches` *are* the rows it wants. If they were
extended onto the task's rows, the file would hold two rows claiming `retriever` and the winner
would be an accident of iteration order — the same defect shape the arms table exists to avoid.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import harness as H  # noqa: E402

COMPOSITE = {
    "id": "retriever",
    "plugin": "forgepilot.providers.retrieve.composite:CompositeRetrieverPlugin",
    "config": {"index_path": "index.json", "roots": {}},
    "tier": "trusted",
}
INDEX_ONLY = {
    "id": "retriever",
    "plugin": "forgepilot.providers.retrieve.index:CodeIndexRetrieverPlugin",
    "config": {"index_path": "index.json"},
    "tier": "trusted",
}


def a_task(**overrides) -> H.EvalTask:
    base = dict(
        name="a-task",
        directory=FORGE / "evals" / "tasks" / "bug-fix",
        fixture=FORGE / "evals" / "tasks" / "bug-fix" / "fixture",
        ticket={},
        expect={},
        patches=(COMPOSITE,),
    )
    base.update(overrides)
    return H.EvalTask(**base)


def test_a_variant_replaces_the_task_patches_rather_than_extending_them() -> None:
    task = a_task(variants={"index-only": {"patches": [INDEX_ONLY]}})

    variant = H.with_variant(task, "index-only")

    assert variant.patches == (INDEX_ONLY,)
    assert COMPOSITE not in variant.patches


def test_a_variant_that_names_no_patches_leaves_the_task_alone() -> None:
    task = a_task(variants={"trusts-the-corpus": {"script": []}})

    variant = H.with_variant(task, "trusts-the-corpus")

    assert variant.patches == (COMPOSITE,)


def test_stale_corpus_really_has_both_rows() -> None:
    """Asserted on the committed task, not on a fixture: the comparison only exists if the
    variant is in the file, and a test that builds its own task would keep passing after the
    row was deleted."""
    task = H.load_task("stale-corpus")

    assert task.patches[0]["plugin"].endswith("CompositeRetrieverPlugin")
    index_only = H.with_variant(task, "index-only")
    assert index_only.patches[0]["plugin"].endswith("CodeIndexRetrieverPlugin")
    # The script is the task's own — the comparison is the retriever row, nothing else.
    assert index_only.script == task.script
