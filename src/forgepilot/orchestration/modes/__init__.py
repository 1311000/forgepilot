"""The three modes, as thin drivers over one backend.

The design is explicit that the modes share a backend and must not fork logic.
These modules are where that is enforced by construction: each one *assembles*
the pieces differently (which context, which adapters, which state machine) and
then hands off to the same loop. If a mode ever needs a branch inside the loop,
that is the signal the difference belongs in configuration instead.
"""

from forgepilot.orchestration.modes.chat import ChatTurn, prepare_chat_turn
from forgepilot.orchestration.modes.review import (
    ReviewPlan,
    build_review,
    prepare_review,
)
from forgepilot.orchestration.modes.task import (
    FIX_PROMPT,
    TaskOutcome,
    TaskPlan,
    plan_task,
    run_task,
)

__all__ = [
    "ChatTurn",
    "prepare_chat_turn",
    "TaskPlan",
    "TaskOutcome",
    "plan_task",
    "run_task",
    "FIX_PROMPT",
    "ReviewPlan",
    "prepare_review",
    "build_review",
]
