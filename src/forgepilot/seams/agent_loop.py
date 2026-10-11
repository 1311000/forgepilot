"""Agent-loop seam — services ``agentLoop`` and ``agents``.

The loop is itself replaceable: a factory is registered under a key and selected
by configuration, which is what makes swapping in the company's own engine a
config change rather than a refactor. That is also why the three modes never fork
loop logic — they differ in wiring, not in an ``if`` inside the loop.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from forgepilot.seams.refs import AgentId, Mode, SessionId, TurnId
from forgepilot.seams.security import Budget, ExecutionConstraints

__all__ = [
    "SERVICE_NAME",
    "AGENTS_SERVICE_NAME",
    "AgentState",
    "AgentSpec",
    "UserInput",
    "TurnResult",
    "Agent",
    "AgentLoop",
    "AgentFactory",
]

SERVICE_NAME = "agentLoop"
AGENTS_SERVICE_NAME = "agents"


class AgentState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Everything needed to instantiate an agent.

    ``constraints`` is carried on the spec rather than looked up later, so an
    agent cannot be created without its authority being stated at creation time.
    """

    agent_id: AgentId
    session_id: SessionId
    mode: Mode
    constraints: ExecutionConstraints
    system_sections: tuple[str, ...] = ()
    budget: Budget = field(default_factory=Budget)


@dataclass(frozen=True, slots=True)
class UserInput:
    text: str
    attachments: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurnResult:
    turn: TurnId
    reason: str = "completed"
    assistant_text: str = ""
    tool_calls: int = 0


class Agent(Protocol):
    @property
    def spec(self) -> AgentSpec: ...

    def state(self) -> AgentState: ...

    async def run_turn(self, user_input: UserInput) -> TurnResult: ...

    def cancel(self, reason: str = "user") -> None: ...


@runtime_checkable
class AgentLoop(Protocol):
    def create(self, spec: AgentSpec) -> Agent: ...


@runtime_checkable
class AgentFactory(Protocol):
    """Registered on ``ctx.agents`` under a key such as ``"forgepilot"``.

    ``kernel_ctx`` is typed ``Any`` deliberately: this module is a seam and must
    not import the plugin kernel, or the loop could never be replaced by an
    implementation that does not use the same container.
    """

    @property
    def key(self) -> str: ...

    def build(self, spec: AgentSpec, kernel_ctx: Any) -> AgentLoop: ...


def agent_factory_keys(factories: Sequence[AgentFactory]) -> tuple[str, ...]:
    """Helper for config validation: the factory keys available to select from."""
    return tuple(factory.key for factory in factories)
