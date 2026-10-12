"""Monotonic guards.

⚠️ **"Guard" is overloaded in this codebase — always qualify it.**

DeepSeek Harness's own ``packages/guard/`` directory holds *loop-hygiene*
plugins (timeouts, repeat-tool reminders). Those are ordinary hooks.

In the security sense used here, a guard is a **monotonic, non-reorderable deny
invariant**: it may refuse a call, may abstain, and can never permit one. Later
phases cannot weaken an earlier guard's refusal, which is what makes the set of
guards the security boundary rather than one input among several.
"""

from forgepilot.runtime.guards.builtin import (
    USAGE_SERVICE,
    UsageTracker,
    command_scope_guard,
    default_guards,
    make_budget_guard,
    path_scope_guard,
    repo_scope_guard,
)
from forgepilot.runtime.guards.registry import (
    Guard,
    GuardDecision,
    GuardRegistry,
    GuardResult,
    GuardSnapshot,
    GuardView,
    make_view,
)

__all__ = [
    "Guard",
    "GuardDecision",
    "GuardRegistry",
    "GuardResult",
    "GuardSnapshot",
    "GuardView",
    "make_view",
    "UsageTracker",
    "USAGE_SERVICE",
    "make_budget_guard",
    "path_scope_guard",
    "repo_scope_guard",
    "command_scope_guard",
    "default_guards",
]
