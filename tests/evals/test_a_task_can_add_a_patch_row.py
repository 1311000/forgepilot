"""A task can add a patch row, and an arm can still override it.

`EvalTask` had `project` — a `project.yaml` override — and no way to change a *plugin row*.
That is the gap a task whose answer sits behind a different `Retriever` falls into: pointing
`retriever` at an index instead of a checkout is a row, not a project value.

⚠️ **The order of the layers is the mechanism, so it is what these tests pin.** Later layers
win, and the requirement is asymmetric: a task must be able to add a row, and an arm must still
be able to override it. An arm is the thing under comparison — a task quietly winning over it
would make an ablation measure the task, which is the failure `test_arms_are_applied.py` exists
because of.

⚠️ **And it is asserted on `run_layers`, the function `boot` actually calls.** Testing a
re-implementation of the order in a test file would pass while the runner did something else,
and that is the same shape as the three arms that were read by nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import harness as H  # noqa: E402


@pytest.fixture(autouse=True)
def the_example_env(tmp_path: Path) -> None:
    """`examples/stat-fix/patch.yaml` expands three variables, so composing its layers needs
    them — the harness's own `export_environment` is what supplies them in a real run, and a
    test that skips this has a `ConfigError` rather than a missing layer, which is the right
    failure but not the one under test."""
    H.export_environment(FORGE, tmp_path / "worktrees")


TASK_ROW = {
    "id": "retriever",
    "plugin": "forgepilot.providers.retrieve.index:CodeIndexRetrieverPlugin",
    "config": {"index_path": "index.json"},
    "tier": "trusted",
}
ARM_ROW = {
    "id": "retriever",
    "plugin": "forgepilot.providers.retrieve.mock:MockRetrieverPlugin",
    "config": {},
    "tier": "trusted",
}


def a_task(*, patches=()) -> H.EvalTask:
    return H.EvalTask(
        name="a-task",
        directory=FORGE / "evals" / "tasks" / "bug-fix",
        fixture=FORGE / "evals" / "tasks" / "bug-fix" / "fixture",
        ticket={},
        expect={},
        patches=tuple(patches),
    )


def test_the_task_layer_is_there_and_named_for_the_task() -> None:
    _, layers = H.run_layers(a_task(patches=(TASK_ROW,)), [], scripted=False)

    assert "--task:a-task" in [layer.name for layer in layers]


def test_the_order_is_example_then_task_then_arm_then_scripted() -> None:
    _, layers = H.run_layers(a_task(patches=(TASK_ROW,)), [ARM_ROW], scripted=True)
    names = [layer.name for layer in layers]

    task_at = names.index("--task:a-task")
    arm_at = names.index("--arm:a-task")
    scripted_at = names.index("--scripted")

    assert task_at < arm_at < scripted_at, names
    # ...and the example's patch is still the base everything is layered on.
    assert task_at > 0


def test_an_arm_row_wins_over_a_task_row() -> None:
    """The asymmetric half: a task may add, and must not be able to outrank the comparison."""
    from forgepilot.config import apply_layers

    profile, layers = H.run_layers(a_task(patches=(TASK_ROW,)), [ARM_ROW], scripted=False)
    resolved = apply_layers(tuple(layers), profile=profile.name)

    retriever = next(row for row in resolved.rows if row.id == "retriever")
    assert retriever.plugin == ARM_ROW["plugin"]


def test_a_task_row_reaches_the_configuration_when_no_arm_overrides_it() -> None:
    """The other half, and the one that would silently do nothing if the wiring were wrong."""
    from forgepilot.config import apply_layers

    profile, layers = H.run_layers(a_task(patches=(TASK_ROW,)), [], scripted=False)
    resolved = apply_layers(tuple(layers), profile=profile.name)

    retriever = next(row for row in resolved.rows if row.id == "retriever")
    assert retriever.plugin == TASK_ROW["plugin"]
    assert retriever.config.get("index_path") == "index.json"


def test_a_task_with_no_patches_adds_no_layer() -> None:
    """The common case does not grow a layer, so an arm's diff stays readable."""
    _, layers = H.run_layers(a_task(), [ARM_ROW], scripted=False)

    assert "--task:a-task" not in [layer.name for layer in layers]
