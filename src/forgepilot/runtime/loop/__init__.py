"""The agent loop.

A **step** is one model request plus the tools it asks for. A **turn** is zero or
more steps, opened by claiming input and closed when nothing is owed.

The loop owns no state of its own: the conversation lives in the session log, and
each step re-derives the request from it. That is what makes a turn resumable in
the only sense this design supports — the log is replayed, not a process
recovered — and it is why the loop can be restarted between steps without losing
anything.
"""

from forgepilot.runtime.loop.agent_loop import (
    AgentFactoryRegistry,
    ForgePilotAgent,
    ForgePilotAgentLoopPlugin,
)
from forgepilot.runtime.loop.step import StepResult, run_step
from forgepilot.runtime.loop.turn import ContextWindowExceeded, run_turn

__all__ = [
    "AgentFactoryRegistry",
    "ForgePilotAgent",
    "ForgePilotAgentLoopPlugin",
    "StepResult",
    "run_step",
    "run_turn",
    "ContextWindowExceeded",
]
