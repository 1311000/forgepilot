"""Task mode planning: ticket in, an executable plan out.

**This stops before execution.** Producing the plan is M7's job; running it is
M8's. Keeping them apart is not ceremony — it is what lets the two invariants be
asserted without a model in the loop, and it makes the plan itself inspectable:
what adapters were chosen, what the model would be told, and which tools were
withheld and why.

The order of the steps is the security property:

1. **Rules first.** A ticket the deterministic preprocessor can reject never
   reaches the classifier, so an incomplete report costs zero model calls.
2. **Classify only what survived.** And only to *understand*, never to decide.
3. **Route from configuration.** The classification picks a *name*; the project
   decides what that name means; the constraints decide what survives.
4. **Assemble context with the trusted/untrusted split.** Ticket-derived text
   goes in the user turn, never the system prompt.

Every early return goes to ``awaiting_info`` — asking a human. There is no path
that guesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from forgepilot.config.project import ProjectConfig
from forgepilot.orchestration.classifier import ClassifierGateway
from forgepilot.orchestration.context_assembly import (
    PromptSection,
    assemble_system_sections,
    compose_task_brief,
)
from forgepilot.orchestration.preprocess import PreprocessResult, preprocess
from forgepilot.orchestration.router import AdapterSelection, select_adapters
from forgepilot.orchestration.task_state import TaskState, TaskStateMachine
from forgepilot.seams.classifier import ClassificationRequest, TicketUnderstanding
from forgepilot.seams.memory import MemoryRecord
from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.tickets import Ticket

__all__ = ["TaskPlan", "plan_task"]


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """What this task would run with, or why it cannot yet run at all."""

    state: TaskState
    ticket: Ticket
    preprocessed: PreprocessResult
    reason: str = ""
    understanding: TicketUnderstanding | None = None
    selection: AdapterSelection | None = None
    system_sections: tuple[PromptSection, ...] = ()
    brief: str = ""
    transitions: tuple[tuple[TaskState, str], ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return self.state is TaskState.QUEUED

    @property
    def needs_human(self) -> bool:
        return self.state is TaskState.AWAITING_INFO

    @property
    def system_texts(self) -> tuple[str, ...]:
        return tuple(section.render() for section in self.system_sections)

    def tool_schemas(self, registry: Any) -> tuple[Any, ...]:
        """Schemas for the selected tools only.

        This is where routing stops being decorative: the loop is handed *only*
        these, so a tool the router withheld is not merely refused later — the
        model never sees it.
        """
        if self.selection is None:
            return ()
        return tuple(
            schema
            for schema in registry.schemas()
            if schema.name in self.selection.tools
        )

    def describe(self) -> str:
        """A short human-readable summary. Used by tests and by the CLI."""
        if self.needs_human:
            return f"awaiting human input: {self.reason}"
        assert self.selection is not None  # noqa: S101 - invariant of `ready`
        withheld = sorted(self.selection.withheld)
        unknown = sorted(self.selection.unknown)
        line = (
            f"ready: tool set {self.selection.tool_set!r}, engine "
            f"{self.selection.engine!r}, {len(self.selection.tools)} tool(s)"
        )
        if withheld:
            line += f"; withheld by constraints: {withheld}"
        if unknown:
            line += f"; not in registry: {unknown}"
        return line


async def plan_task(
    *,
    ticket: Ticket,
    project: ProjectConfig,
    constraints: ExecutionConstraints,
    registry: Any,
    classifier: ClassifierGateway,
    workspace_root: Path | None = None,
    memories: Sequence[MemoryRecord] = (),
) -> TaskPlan:
    """Preprocess, classify if needed, route, and assemble. Executes nothing."""
    machine = TaskStateMachine()

    preprocessed = preprocess(ticket, known_repos=project.repos)

    if preprocessed.needs_human:
        # The whole point: an incomplete ticket is rejected by rules alone, so
        # the classifier is never consulted.
        machine.to(TaskState.AWAITING_INFO, preprocessed.reason)
        return TaskPlan(
            state=TaskState.AWAITING_INFO,
            ticket=ticket,
            preprocessed=preprocessed,
            reason=preprocessed.reason,
            transitions=tuple(machine.history),
        )

    understanding = await classifier.classify(
        ClassificationRequest(
            ticket_id=ticket.id,
            title=ticket.title,
            description=ticket.description_md,
            comments=tuple(c.body_md for c in ticket.comments),
            signals=dict(preprocessed.signals),
        )
    )

    if understanding.needs_human:
        # A classifier that is unsure, or that noticed something missing, sends
        # the task to a person. It never fills the gap with a guess.
        reason = (
            f"classifier confidence {understanding.confidence:.2f}"
            + (f"; could not determine: {', '.join(understanding.missing)}" if understanding.missing else "")
        )
        machine.to(TaskState.AWAITING_INFO, reason)
        return TaskPlan(
            state=TaskState.AWAITING_INFO,
            ticket=ticket,
            preprocessed=preprocessed,
            understanding=understanding,
            reason=reason,
            transitions=tuple(machine.history),
        )

    selection = select_adapters(
        understanding=understanding,
        project=project,
        constraints=constraints,
        registry=registry,
    )
    sections = assemble_system_sections(
        project=project, constraints=constraints, workspace_root=workspace_root
    )
    brief = compose_task_brief(
        ticket=ticket,
        understanding=understanding,
        preprocessed=preprocessed,
        memories=memories,
    )

    return TaskPlan(
        state=TaskState.QUEUED,
        ticket=ticket,
        preprocessed=preprocessed,
        understanding=understanding,
        selection=selection,
        system_sections=sections,
        brief=brief,
        reason="planned",
        transitions=tuple(machine.history),
    )
