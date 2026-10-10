"""The guards every session runs with.

These are *security-context* guards. They run on the normalised
:class:`~forgepilot.seams.tools.ResourceTargets` a tool definition extracted, so
none of them parses argument JSON — one place knows that ``{"file": ...}`` means
a path, and every guard then reasons about paths.

A guard is a pure predicate: it is handed a view and returns abstain or deny. It
holds no handles and performs no I/O, which is what makes it safe to evaluate
before anything has been approved.
"""

from __future__ import annotations

from dataclasses import dataclass

from forgepilot.runtime.guards.registry import Guard, GuardDecision, GuardResult, GuardView
from forgepilot.seams.security import normalise_relative
from forgepilot.seams.tools import ToolEffect

__all__ = [
    "operation_guard",
    "repo_scope_guard",
    "path_scope_guard",
    "UsageTracker",
    "USAGE_SERVICE",
    "make_budget_guard",
    "default_guards",
]


def operation_guard(view: GuardView) -> GuardResult:
    """Refuse a call whose declared operation the caller may not perform.

    This is what makes ``permitted_operations`` load-bearing. Without it the
    allow-list would be recorded, snapshotted, dumped by ``--dump-config`` and
    consulted by nothing — the most dangerous kind of security field, because it
    looks enforced.

    The operation is declared on the tool definition rather than inferred from
    its effects: "writes a file" and "opens a pull request" can share an effect
    while needing different permission.
    """
    required = view.required_operation
    if required is None:
        return GuardResult()
    if not view.constraints.allows_operation(required):
        return GuardResult(
            decision=GuardDecision.DENY,
            reason=(
                f"operation {getattr(required, 'value', required)!r} is not permitted "
                f"for this principal"
            ),
            guard="operation",
        )
    return GuardResult()


def repo_scope_guard(view: GuardView) -> GuardResult:
    """Refuse any repository outside the caller's allow-list.

    This is the guard that makes ``allowed_repos`` load-bearing rather than
    advisory: even if a tool, a prompt or a bug tried to reach another repo, the
    call cannot proceed.
    """
    offending = [repo for repo in view.targets.repos if not view.constraints.allows_repo(repo)]
    if offending:
        return GuardResult(
            decision=GuardDecision.DENY,
            reason=(
                f"repo(s) {[getattr(r, 'slug', str(r)) for r in offending]} are outside the "
                f"caller's permitted set"
            ),
            guard="repo-scope",
        )
    return GuardResult()


def path_scope_guard(view: GuardView) -> GuardResult:
    """Refuse escapes from the workspace, and writes outside the writable set.

    Two distinct checks, deliberately not merged:

    - **Containment** applies to every call whatever its effects. A path that
      normalises to ``None`` is one that escaped the root (``../`` or absolute),
      and there is no tool for which that is acceptable.
    - **Permission** applies only to calls that would write. Reading outside
      ``writable_paths`` is not itself a breach — reads are bounded by the
      workspace root and the repo allow-list instead.
    """
    writes = ToolEffect.WRITE in view.effects

    for path in view.targets.paths:
        if not view.constraints.allows_write_to(path) and writes:
            return GuardResult(
                decision=GuardDecision.DENY,
                # The set is named. A refusal that does not say what *would* have
                # been allowed leaves the caller — model or operator — guessing:
                # a real run had the model try eleven spellings of the same path
                # before giving up, each answered identically.
                reason=(
                    f"path {path!r} is outside the caller's writable set "
                    f"({sorted(view.constraints.writable_paths) or 'nothing is writable'})"
                ),
                guard="path-scope",
            )
        if _escapes_workspace(path):
            return GuardResult(
                decision=GuardDecision.DENY,
                reason=f"path {path!r} escapes the workspace root",
                guard="path-scope",
            )
    return GuardResult()


def _escapes_workspace(path: str) -> bool:
    return normalise_relative(path) is None


@dataclass
class UsageTracker:
    """Running totals for one turn. Owned by the loop; read by the budget guard."""

    tool_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0

    # ⚠️ `record_call()` used to live here and was called by nothing. The loop
    # counts with `tracker.tool_calls += len(result.tool_calls)`, so the method was
    # not merely dead — it made the tracker look like an object whose counting goes
    # through a method, which is a large part of how the two-tracker defect below
    # stayed invisible. Deleted rather than kept "in case".


#: The name this tracker is provided under inside a request scope.
#:
#: ⚠️ **It has to be reachable, and that is not a convenience.** The guards are
#: built around the scope's tracker, so anything that *counts* has to write to the
#: same object or it is counting into a private one. `ForgePilotAgent` used to
#: default its own, the loop incremented that, and the budget guard watched a
#: tracker nothing touched — so the guard refused nothing while the loop's own
#: between-step check went on working, which is exactly the shape that hides.
USAGE_SERVICE = "usage"


@dataclass(frozen=True, slots=True)
class _BudgetLimits:
    max_tool_calls: int
    max_tokens: int
    max_cost_usd: float
    max_wall_seconds: float


def make_budget_guard(tracker: UsageTracker, *, guard_name: str = "budget") -> Guard:
    """A guard that refuses once any budget dimension is exhausted.

    A guard rather than a hook because exceeding a budget must be **final**: a
    later phase re-allowing the call would mean the limit was decorative.
    """

    def guard(view: GuardView) -> GuardResult:
        limits = _BudgetLimits(
            max_tool_calls=view.constraints.budget.max_tool_calls,
            max_tokens=view.constraints.budget.max_tokens,
            max_cost_usd=view.constraints.budget.max_cost_usd,
            max_wall_seconds=view.constraints.budget.max_wall_seconds,
        )
        if tracker.tool_calls >= limits.max_tool_calls:
            return GuardResult(
                decision=GuardDecision.DENY,
                reason=f"tool-call budget exhausted ({limits.max_tool_calls})",
                guard=guard_name,
            )
        if tracker.tokens >= limits.max_tokens:
            return GuardResult(
                decision=GuardDecision.DENY,
                reason=f"token budget exhausted ({limits.max_tokens})",
                guard=guard_name,
            )
        if tracker.cost_usd >= limits.max_cost_usd:
            return GuardResult(
                decision=GuardDecision.DENY,
                reason=f"cost budget exhausted (${limits.max_cost_usd})",
                guard=guard_name,
            )
        if tracker.wall_seconds >= limits.max_wall_seconds:
            return GuardResult(
                decision=GuardDecision.DENY,
                reason=f"wall-clock budget exhausted ({limits.max_wall_seconds}s)",
                guard=guard_name,
            )
        return GuardResult()

    return guard


def default_guards(tracker: UsageTracker | None = None) -> list[tuple[str, Guard]]:
    """The guards a session should start with, in evaluation order.

    Repo and path scope come first: refusing a call that was never permitted is
    more informative than refusing it for budget, and the reason the caller sees
    should be the most fundamental one.
    """
    return [
        ("operation", operation_guard),
        ("repo-scope", repo_scope_guard),
        ("path-scope", path_scope_guard),
        ("budget", make_budget_guard(tracker or UsageTracker())),
    ]
