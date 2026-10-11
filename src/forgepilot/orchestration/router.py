"""Choosing adapters — the place where "the model proposes" could become "the model decides".

The chain is::

    ticket -> deterministic preprocessing -> **classification** -> routing -> tools

Classification is model output derived from untrusted ticket text, and routing
decides which tools exist. If a classifier's suggestion could *add* a tool, a
prompt-injected ticket saying "this needs shell access" would escalate privilege
past every guard, sandbox and constraint built in M4 and M5.

So the rule is stronger than "intersect at the end":

  **The model may only narrow. It can never add.**

Concretely, the candidate set comes from two places that have nothing to do with
the model — the project's configured tool set, and the caller's constraints — and
the understanding is used for exactly one thing: **choosing which configured set
applies**. A classification says "this looks like a bugfix", and the *project*
decides what a bugfix is allowed to use. A category the project has not
configured falls back to the default set, never to "everything".

Note what this buys: even a fully attacker-controlled understanding cannot reach a
tool this caller could not already have used. The worst it can do is pick the
wrong pre-approved set.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from forgepilot.config.project import ProjectConfig
from forgepilot.seams.classifier import TicketUnderstanding
from forgepilot.seams.security import ExecutionConstraints

__all__ = ["AdapterSelection", "select_adapters", "permitted_tools"]


@dataclass(frozen=True, slots=True)
class AdapterSelection:
    """What this task will run with. Every field is traceable to config or constraints."""

    tool_set: str
    engine: str
    tools: frozenset[str]
    #: Configured for the chosen set, but the caller's constraints refuse them.
    #: Surfaced rather than hidden: a tool that silently vanishes is a support
    #: ticket waiting to happen.
    withheld: frozenset[str] = frozenset()
    #: Configured names the registry does not have. Almost always a typo in
    #: `project.yaml`, and invisible unless reported.
    unknown: frozenset[str] = frozenset()
    system_sections: tuple[str, ...] = field(default_factory=tuple)


def permitted_tools(
    names: Sequence[str],
    constraints: ExecutionConstraints,
    registry: Any,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Split ``names`` into (permitted, withheld, unknown).

    A tool is permitted when the operation it declares is one the caller holds —
    or when it declares none, which only inert tools should. Unknown names are
    reported rather than dropped so a configuration typo does not read as "the
    project simply does not use that tool".
    """
    permitted: set[str] = set()
    withheld: set[str] = set()
    unknown: set[str] = set()

    for name in names:
        definition = registry.get(name)
        if definition is None:
            unknown.add(name)
            continue
        required = getattr(definition, "required_operation", None)
        if required is None or constraints.allows_operation(required):
            permitted.add(name)
        else:
            withheld.add(name)

    return frozenset(permitted), frozenset(withheld), frozenset(unknown)


def select_adapters(
    *,
    understanding: TicketUnderstanding | None,
    project: ProjectConfig,
    constraints: ExecutionConstraints,
    registry: Any,
) -> AdapterSelection:
    """Resolve the tool set and engine for one task.

    ``understanding`` is optional and, when present, is used **only** to look up a
    configured set. It cannot contribute a tool name, and there is no branch in
    this function that reads a tool from it.
    """
    category = (understanding.category if understanding else "") or ""

    # A category the project has not configured falls back to the default set —
    # never to the union of everything.
    set_name = project.category_tool_sets.get(category, project.default_tool_set)
    if set_name not in project.tool_sets:
        set_name = project.default_tool_set

    configured = project.tool_sets.get(set_name, ())
    allowed, withheld, unknown = permitted_tools(configured, constraints, registry)

    engine = project.category_engines.get(category, project.default_engine)

    return AdapterSelection(
        tool_set=set_name,
        engine=engine,
        tools=allowed,
        withheld=withheld,
        unknown=unknown,
        system_sections=(),
    )
