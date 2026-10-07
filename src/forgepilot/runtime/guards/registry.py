"""The guard registry.

Two properties are enforced structurally rather than by convention, because both
are the kind that a well-meaning later change would quietly break:

**Monotonic.** :class:`GuardDecision` has no ``allow`` member. A guard can abstain
or deny; there is nothing else it could return, so no guard can re-permit what
another refused, and no ordering of guards can produce an allow. Adding an
``ALLOW`` variant later would be the change that turns the security boundary into
a suggestion — don't.

**Snapshotted.** The set of guards for a call is captured once, at ``tool/call``
time. A plugin that registers a guard while a call is in flight does not affect
that call, and a plugin cannot *unregister* one to get out from under it either.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.tools import ResourceTargets

__all__ = [
    "GuardDecision",
    "GuardResult",
    "Guard",
    "GuardView",
    "GuardRegistry",
    "GuardSnapshot",
    "make_view",
]


class GuardDecision(StrEnum):
    """A guard's verdict. Deliberately closed and deliberately not including allow."""

    ABSTAIN = "abstain"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class GuardResult:
    decision: GuardDecision = GuardDecision.ABSTAIN
    reason: str = ""
    guard: str = ""


@dataclass(frozen=True, slots=True)
class GuardView:
    """What a guard is shown.

    No handles and no I/O: a guard is a pure predicate over this. That is what
    lets guards run in the same phase as pre-execute hooks without any of them
    being able to cause an effect before approval.
    """

    tool: str
    effects: frozenset[str]
    targets: ResourceTargets
    arguments: dict
    constraints: ExecutionConstraints
    #: The permission the tool declared it needs, if any.
    required_operation: Any = None


Guard = Callable[[GuardView], GuardResult]


@dataclass(frozen=True, slots=True)
class GuardSnapshot:
    """The guards in force for one call, fixed at ``tool/call`` time."""

    guards: tuple[tuple[str, Guard], ...]

    def evaluate(self, view: GuardView) -> GuardResult:
        """First denial wins; every guard is consulted only until one refuses."""
        for name, guard in self.guards:
            result = guard(view)
            if result.decision is GuardDecision.DENY:
                return GuardResult(
                    decision=GuardDecision.DENY,
                    reason=result.reason or f"denied by {name}",
                    guard=result.guard or name,
                )
        return GuardResult()


class GuardRegistry:
    """An append-only, ordered set of guards."""

    def __init__(self) -> None:
        self._guards: list[tuple[str, Guard]] = []

    def register(self, name: str, guard: Guard) -> Callable[[], None]:
        """Add a guard. Returns a callable that removes it again.

        Removal exists for plugin teardown, not for escaping a guard mid-call:
        an in-flight call holds a snapshot and is unaffected either way.
        """
        entry = (name, guard)
        self._guards.append(entry)
        return lambda: self._guards.remove(entry) if entry in self._guards else None

    def snapshot(self) -> GuardSnapshot:
        return GuardSnapshot(guards=tuple(self._guards))

    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self._guards)

    def __len__(self) -> int:
        return len(self._guards)


def make_view(
    *,
    tool: str,
    effects: frozenset[str],
    targets: ResourceTargets,
    arguments: dict,
    constraints: ExecutionConstraints,
    required_operation: Any = None,
) -> GuardView:
    return GuardView(
        tool=tool,
        effects=effects,
        targets=targets,
        arguments=arguments,
        constraints=constraints,
        required_operation=required_operation,
    )
