"""One mechanism switched off at a time — and, once, all of them together.

The design doc's claim is that this system is better than a generic agent. There
is no way to run the generic agent here, so the question this answers instead is
the one that is actually answerable and actually useful: **does each mechanism
earn its place?** Run the same tasks with one thing disabled and compare.

`baseline` is the one arm that is not "one thing disabled": it turns off every
mechanism this harness implements and can switch off, so it answers the aggregate
question the per-mechanism arms average away. It is what "a generic agent" is made
of **inside this harness** — not a measurement of anybody else's product.

An arm is deliberately small and declarative — a project override and at most a
patch row — so that "what was different" is one glance rather than a second
program. If an arm needs more than this to express, the mechanism under test is
probably not switchable, and that is worth knowing too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Arm:
    name: str
    description: str
    #: Merged over the task's project mapping, shallowly — a key given here
    #: replaces that key entirely, the same rule the config plane uses.
    project: dict[str, Any] = field(default_factory=dict)
    #: Extra patch rows, applied after the example's patch.
    patches: tuple[dict[str, Any], ...] = ()
    #: Whether a run **recalls** what earlier runs learned. ⚠️ **A runtime switch,
    #: and `project`/`patches` cannot express it** — the config plane says what the
    #: memory store *is*, never whether this run reads it. So it is a field here,
    #: and `H.arm_changes` reports it like any other switch: the point of the two
    #: fields is that an arm that changed nothing must still be visible as an arm
    #: that changed nothing, or the comparison is the baseline against itself.
    memory: bool = True
    #: Whether a finished run is distilled into that store. Separate from `memory`
    #: because "learned nothing" and "read nothing it had learned" are different
    #: failures, and only one of them shows up as an empty prompt block.
    distill: bool = True


#: The tool sets without `code_search`, spelled out in full because an override
#: replaces the whole key. A deployment that has no retriever loses this tool
#: anyway (it lives in its own plugin for exactly that reason), so this arm
#: reproduces what a profile without one looks like.
_SETS_WITHOUT_SEARCH = {
    "default": ["read_file"],
    "bugfix": ["read_file", "write_file", "run"],
}

#: The tool sets with the retriever and without `run`, spelled out for the same
#: reason an override replaces the whole key.
#:
#: ⚠️ **This arm is an instrument, not an ablation of a mechanism.** It exists
#: because `stale-corpus` could not price the composite: the task asks for a helper
#: the index does not hold, and two live attempts measured the composite and the
#: index alone at the *same* 3/3 — because the agent has `run`, so `dir` and
#: `findstr` are a second discovery channel and the retriever was never the only way
#: to find the file (`docs/notes/evals.md`, "Pricing the composite"). Remove the
#: shell and keep the retriever and the question becomes answerable: a corpus-only
#: agent cannot find `paging/_shared_guard.py`, and the held-out probe's reuse check
#: says so. It is also the shape a GVFS checkout actually has — metadata local, no
#: shell over the tree — which is why it is worth having beyond one task.
_SETS_WITHOUT_SHELL = {
    "default": ["read_file", "code_search", "read_code"],
    "bugfix": ["read_file", "code_search", "read_code", "write_file", "edit_file"],
}


ARMS: dict[str, Arm] = {
    "full": Arm(
        name="full",
        description="everything on — the reference every other arm is compared against",
    ),
    # ⚠️ **The control, and it is not a competitor.** Every other arm switches one
    # mechanism off; this one switches off all three that this harness implements and
    # can disable, leaving a plain read/write/run loop with a category-chosen tool
    # set. It is the *aggregate* question the single-mechanism arms average away:
    # do these earn their place **together**?
    #
    # Deliberately **not** a comparison against Claude Code or any other product.
    # There is no such arm here, and inventing one would be the same unfounded claim
    # the eval set exists to refuse — the doc's own +32% is recorded as unverifiable
    # for exactly this reason. What this measures is a delta *within* this harness.
    #
    # Composed of the three project overrides below rather than of new machinery: an
    # override replaces its key wholesale, so naming all three keys **is** the union
    # of `no-search`, `no-pinning` and `no-repair`. `H.arm_changes` diffs it against
    # `full` and every row records the result, which is what makes "it switched three
    # things off" checkable rather than asserted.
    "baseline": Arm(
        name="baseline",
        description=(
            "no code_search, no held oracle, no repair loop — the plain loop with "
            "nothing this harness adds on top of it"
        ),
        project={
            "tool_sets": _SETS_WITHOUT_SEARCH,
            "test_paths": [],
            "retry": {"max_attempts": 0, "on_failure": "stop"},
        },
    ),
    "no-search": Arm(
        name="no-search",
        description="no code_search: the agent must already know which file to read",
        project={"tool_sets": _SETS_WITHOUT_SEARCH},
    ),
    "no-shell": Arm(
        name="no-shell",
        description=(
            "no `run`: discovery goes through the retriever, the shape a GVFS checkout has"
        ),
        project={"tool_sets": _SETS_WITHOUT_SHELL},
    ),
    "no-pinning": Arm(
        name="no-pinning",
        description="the check runs whatever tests are in the tree, including edited ones",
        project={"test_paths": []},
    ),
    "no-repair": Arm(
        name="no-repair",
        description="one attempt: a failing check ends the task instead of looping",
        project={"retry": {"max_attempts": 0, "on_failure": "stop"}},
    ),
    # ⚠️ **The pair that measures the memory milestone, and they are two arms rather
    # than one because the two halves fail differently.** `no-memory` recalls nothing
    # while still learning — the run after it in the same arm's store has something to
    # read and does not read it. `no-distill` reads nothing while still being written
    # to, which is what it would be if the distillation step silently did not run: the
    # prompt block is empty, the store is empty, and only the row can tell those apart.
    "no-memory": Arm(
        name="no-memory",
        description="recall off: earlier runs in this repository are learned from and never read",
        memory=False,
    ),
    "no-distill": Arm(
        name="no-distill",
        description="distillation off: nothing the earlier runs learned ever reaches the store",
        distill=False,
    ),
    "tight-window": Arm(
        name="tight-window",
        description="a model window small enough that compaction has to work for any task to finish",
        # Every key is spelled out because a config override replaces the whole
        # block, not the one key named — the trap `user.patch.yaml` warns about.
        patches=(
            {
                "id": "llm",
                "plugin": "forgepilot.providers.llm.openai_compat:OpenAICompatLLMPlugin",
                "config": {
                    "model": "${FORGEPILOT_LLM_MODEL:-gpt-4o-mini}",
                    "base_url": "${FORGEPILOT_LLM_BASE_URL:-https://api.openai.com/v1}",
                    "api_key_env": "FORGEPILOT_LLM_API_KEY",
                    # Small enough that a two-file conversation crosses the
                    # trigger. The default is 128k, where nothing in this task
                    # set would ever reach it — an arm that never fires measures
                    # nothing.
                    "max_context_tokens": 6000,
                },
                "tier": "trusted",
            },
        ),
    ),
}


def arm(name: str) -> Arm:
    try:
        return ARMS[name]
    except KeyError:
        raise KeyError(f"no eval arm {name!r}. Known: {sorted(ARMS)}") from None


def names() -> tuple[str, ...]:
    return tuple(ARMS)
