"""Chat mode: a question about the codebase.

The shortest path through the orchestration layer, and the one that demonstrates
the second invariant most plainly — there is no ticket, so there is nothing to
classify, and the classifier is **not called at all**. A query is answered by
retrieval and reasoning, not by deciding what kind of work it is.

Preparation is separated from execution so the assembly can be asserted on
without running a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forgepilot.config.project import ProjectConfig
from forgepilot.orchestration.context_assembly import (
    PromptSection,
    assemble_system_sections,
)
from forgepilot.seams.memory import MemoryRecord
from forgepilot.seams.security import ExecutionConstraints

__all__ = ["ChatTurn", "prepare_chat_turn"]


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """Everything a chat turn needs, assembled."""

    system_sections: tuple[PromptSection, ...]
    question: str
    memories: tuple[MemoryRecord, ...] = ()

    @property
    def system_texts(self) -> tuple[str, ...]:
        return tuple(section.render() for section in self.system_sections)


def prepare_chat_turn(
    *,
    question: str,
    project: ProjectConfig,
    constraints: ExecutionConstraints,
    workspace_root: Path | None = None,
    memories: tuple[MemoryRecord, ...] = (),
) -> ChatTurn:
    """Assemble a chat turn.

    Note what is absent: no preprocessing, no classification, no routing. A
    question is not a task, and running it through ticket machinery would be both
    wasted work and a wasted model call — which is exactly the claim
    ``test_a_query_never_reaches_the_classifier`` checks.
    """
    sections = assemble_system_sections(
        project=project, constraints=constraints, workspace_root=workspace_root
    )
    return ChatTurn(system_sections=sections, question=question, memories=memories)
