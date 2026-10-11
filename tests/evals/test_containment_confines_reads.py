"""Containment has to confine *reads*, or `no-search` measures nothing.

`no-search` removes the `code_search` tool and claims the answer is therefore out of
reach. It was not: the kernel sandbox bound `/` read-only, which is a boundary you can
*see* rather than one you cannot reach, so a `run` command could `cat` the answer out of
an index sitting beside the task — three of three contained `no-search` rows did exactly
that, and each one was recorded `aside` rather than scored. See finding 20 in
`evals/README.md`.

Two things have to hold for the claim to be true, and neither is visible from the arm
table:

1. the containment row must *claim* `read_scope: workspace` — a row that manages writes
   and claims nothing about reads checks out identically;
2. the claim must reach the configuration a run actually loads, and it must sit **above**
   the arm, because an arm that could switch containment off would be measuring
   containment rather than itself.

⚠️ **Asserted on `run_layers` and on the resolved configuration, not on the literal
above it.** The lesson this eval already paid for is that a mechanism read by nothing
looks exactly like a mechanism that is not there — three of five arms ran the baseline
under a different name for a whole measurement cycle.
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
    """`examples/stat-fix/patch.yaml` expands three variables, so composing its layers
    needs them — the harness's own `export_environment` is what supplies them in a real
    run, and a test that skips this gets a `ConfigError` instead of a missing layer."""
    H.export_environment(FORGE, tmp_path / "worktrees")


ARM_ROW = {
    "id": "sandbox",
    "plugin": "forgepilot.providers.sandbox.scripted:ScriptedSandboxPlugin",
    "config": {},
    "tier": "trusted",
}


def a_task() -> H.EvalTask:
    return H.EvalTask(
        name="a-task",
        directory=FORGE / "evals" / "tasks" / "bug-fix",
        fixture=FORGE / "evals" / "tasks" / "bug-fix" / "fixture",
        ticket={},
        expect={},
        patches=(),
    )


def test_the_containment_row_claims_that_reads_are_confined() -> None:
    """The claim itself. `host` is the default everywhere else *because it is what the
    bundle ships* — a deployment that confines reads pays for it (every unnamed
    top-level entry becomes an empty tmpfs), so the eval asking for it has to say so
    rather than inherit it from the deployment.
    """
    rows = {row["id"]: row for row in H.CONTAINMENT_ROWS}

    assert rows["sandbox"]["config"]["read_scope"] == "workspace", (
        "containment confines writes but claims nothing about reads, so a contained "
        "no-search row can still read the answer out of the host"
    )


def test_the_claim_reaches_the_configuration_the_run_loads() -> None:
    """End of the chain, and the link that is easy to leave dangling: a row that is right
    on paper and expanded away, or overridden by a later layer, is the same as no row."""
    from forgepilot.config import apply_layers

    profile, layers = H.run_layers(a_task(), [], scripted=False, contain=True)
    resolved = apply_layers(tuple(layers), profile=profile.name)

    sandbox = next(row for row in resolved.rows if row.id == "sandbox")
    assert sandbox.config.get("read_scope") == "workspace", sandbox.config


def test_containment_outranks_an_arm_that_would_switch_it_off() -> None:
    """The asymmetric half, made explicit for this layer the way it is for a task row.

    An arm is the thing under comparison. If an arm could replace the containment row,
    a contained sweep would be measuring the arm's sandbox rather than the arm.
    """
    from forgepilot.config import apply_layers

    profile, layers = H.run_layers(a_task(), [ARM_ROW], scripted=False, contain=True)
    names = [layer.name for layer in layers]

    assert names.index("--contain") > names.index("--arm:a-task"), names
    resolved = apply_layers(tuple(layers), profile=profile.name)
    sandbox = next(row for row in resolved.rows if row.id == "sandbox")
    assert sandbox.plugin == "forgepilot.providers.sandbox.namespace:NamespaceSandboxPlugin"


def test_an_uncontained_run_still_says_so_by_having_no_containment_layer() -> None:
    """The control. `--contain` present in every run would make the two sweeps in
    `evals/README.md` the same sweep with two names."""
    _, layers = H.run_layers(a_task(), [ARM_ROW], scripted=False, contain=False)

    assert "--contain" not in [layer.name for layer in layers]
