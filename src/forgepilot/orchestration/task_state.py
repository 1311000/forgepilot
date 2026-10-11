"""The task state machine.

A task is a thing with a lifecycle, and naming the states is what makes
"what happened to this ticket?" answerable without reading a transcript.

Two of the states exist because of how this system is designed rather than
because a workflow needs them:

- **``awaiting_info``** — the harness asked a human for something it could not
  determine. Reaching it is a *success* of the rules, not a failure: the
  alternative was guessing.
- **``awaiting_approval``** — a human must answer before an effect happens.

Transitions are checked. An illegal one raises rather than being applied, because
the states that matter here are the ones that gate effects, and a task that
silently skipped ``awaiting_approval`` would be a security bug wearing a workflow
costume.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from forgepilot.errors import ForgePilotError

__all__ = ["TaskState", "IllegalTransition", "TaskStateMachine", "ALLOWED_TRANSITIONS"]


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    #: Waiting on a person to supply information the rules could not derive.
    AWAITING_INFO = "awaiting_info"
    #: Waiting on a person to answer an approval prompt.
    AWAITING_APPROVAL = "awaiting_approval"
    DELIVERED = "delivered"
    #: The work is done — checks passed, the commit exists, the draft is written —
    #: and it has **not** been published, because no repo host is configured.
    #:
    #: Distinct from :attr:`DELIVERED` on purpose. Publishing needs a service that
    #: owns pull requests; without one there is nothing to open against, and
    #: calling that "delivered" would be the same lie as a mock reporting success
    #: for work it did not do. Distinct from :attr:`FAILED` too: nothing failed.
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.AWAITING_INFO, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.AWAITING_INFO: frozenset({TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.AWAITING_INFO,
            TaskState.AWAITING_APPROVAL,
            TaskState.DELIVERED,
            TaskState.READY,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.AWAITING_APPROVAL: frozenset(
        {TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED}
    ),
    # Terminal.
    TaskState.DELIVERED: frozenset(),
    TaskState.READY: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


class IllegalTransition(ForgePilotError):
    """A transition that the state machine does not permit."""


@dataclass
class TaskStateMachine:
    state: TaskState = TaskState.QUEUED
    #: Every transition taken, with its reason. Kept in memory for the caller;
    #: the durable record is the `task/state` session event.
    history: list[tuple[TaskState, str]] = field(default_factory=list)

    def can(self, target: TaskState) -> bool:
        return target in ALLOWED_TRANSITIONS[self.state]

    def to(self, target: TaskState, reason: str = "") -> TaskState:
        if not self.can(target):
            raise IllegalTransition(
                f"cannot move a task from {self.state.value!r} to {target.value!r}. "
                f"Allowed from here: {sorted(s.value for s in ALLOWED_TRANSITIONS[self.state])}"
            )
        self.state = target
        self.history.append((target, reason))
        return self.state

    @property
    def is_terminal(self) -> bool:
        return not ALLOWED_TRANSITIONS[self.state]

    @property
    def is_waiting_on_a_human(self) -> bool:
        return self.state in {TaskState.AWAITING_INFO, TaskState.AWAITING_APPROVAL}
