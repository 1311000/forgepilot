"""The pre-execute phase: where decisions happen and nothing else may.

This module exists because of a specific, documented defect. In DeepSeek Harness
(discussion #1863), a ``tools/pre-execute`` listener is ordinary plugin code with
ambient host privileges, and it must **run to completion** in order to return
``ask``. So a listener can spawn a process, let it exit, and only then ask the
human — and denying afterwards cannot undo what already ran.

ForgePilot's answer has two halves:

1. **Structural**: :class:`PreExecuteScope` is a frozen, slotted dataclass holding
   a logger and the caller's constraints. There is no filesystem handle, no
   process API, no network client, no sandbox, no workspace. A hook that wants to
   spawn something has to reach for a capability it was never handed.
2. **Honest about the limit**: in-process Python cannot give a plugin a *real*
   capability boundary — `import subprocess` still works. The scope narrows what
   a listener is given and catches mistakes; it does not contain hostile code.
   Containing hostile code is why third-party plugins run out of process, and
   that is the boundary to rely on for anything untrusted.

So this is a mistake-preventer, not a sandbox, and it is labelled as such rather
than oversold.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from forgepilot.seams.security import ExecutionConstraints, Operation, Principal
from forgepilot.seams.tools import ResourceTargets, ToolEffect

__all__ = [
    "CallPreview",
    "PreExecuteScope",
    "PreDecisionKind",
    "PreDecision",
    "PreExecuteListener",
    "ALLOW_ALL",
]


@dataclass(frozen=True, slots=True)
class CallPreview:
    """What a pre-execute listener is told about the call.

    Read-only facts only: the parsed arguments, the tool's declared effects, the
    normalised targets, and the workspace root. Nothing here can act.
    """

    tool: str
    effects: frozenset[ToolEffect]
    arguments: Mapping[str, Any]
    arguments_json: str
    targets: ResourceTargets
    workspace_root: Path | None = None
    #: The ``Operation`` this tool maps to — ``read_code``, ``run_tests``,
    #: ``open_pr``. **Declared by the tool, never inferred from its effects.**
    #:
    #: Without it a listener cannot ask "may this principal do this *kind* of
    #: thing": "writes a file" and "opens a pull request" can share an effect
    #: while needing different permission, which is exactly why
    #: `ToolDefinition.required_operation` exists as its own field. It was simply
    #: not carried this far, so the one phase whose job is to decide had nothing
    #: to decide *about*.
    required_operation: Operation | None = None


@dataclass(frozen=True, slots=True)
class PreExecuteScope:
    """The entire world a pre-execute listener may touch.

    Frozen and slotted so nothing can be attached to it at runtime, and holding
    deliberately little. If you find yourself wanting to add a handle here, that
    is the signal that the work belongs in :mod:`forgepilot.runtime.tools.arm`,
    which runs after the human has been asked.

    Note the absence of anything resembling: ``sandbox``, ``workspace``,
    ``session``, ``ctx``, ``open``, ``subprocess``, ``httpx``. A regression test
    asserts those names are absent, because "we did not add one yet" is a
    property worth freezing.

    ``principal`` is **data, not a capability**, and it is here because the phase
    could not otherwise do its job. A permission check that cannot see *who* is
    calling is not a permission check; `ExecutionConstraints` carries the
    principal's id and tenant, so the identity was already visible here — this
    hands over the object rather than making every listener rebuild one.
    """

    logger: Any
    constraints: ExecutionConstraints
    principal: Principal


class PreDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    #: Escalate to a human. One-shot and fail-closed: no answer means refusal.
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class PreDecision:
    kind: PreDecisionKind
    reason: str = ""


#: Returning ``None`` means "no opinion" — deliberately distinct from
#: :attr:`PreDecisionKind.ALLOW`, so a listener that simply does not care is not
#: recorded as having permitted the call.
PreExecuteListener = Callable[[CallPreview, PreExecuteScope], "PreDecision | None"]

ALLOW_ALL = PreDecision(kind=PreDecisionKind.ALLOW, reason="no listener objected")


def resolve_decision(
    listeners: tuple[PreExecuteListener, ...],
    preview: CallPreview,
    scope: PreExecuteScope,
) -> PreDecision:
    """Run listeners in order; the first opinion wins.

    An explicit ordered chain rather than a Cordis waterfall, and that is a
    deliberate departure from the reference design. The port's waterfall does not
    thread values forward — ``next(value)`` discards its argument — so a decision
    could only be carried back by return values, and a listener that forgets to
    call ``next`` silently ends the chain. For a first-decision-wins permission
    check, none of that machinery buys anything and all of it can break quietly.
    """
    for listener in listeners:
        decision = listener(preview, scope)
        if decision is not None:
            return decision
    return ALLOW_ALL
