"""The loop as a service, and the factory that makes it swappable.

The indirection through a factory is the whole point of this module. The engine
is selected by a **key in configuration**, so replacing the built-in loop with
the company's own — or with an external engine driven over a subprocess — is a
patch line rather than a refactor. That is also why the three modes never fork
loop logic: they differ in wiring, not in an ``if`` inside the loop.

Nothing here resolves a service at import time. The loop is built **against a
context**, and the caller passes the one it means — normally a request scope, so
the loop's services and its constraints come from the same place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.kernel.errors import PluginContractViolation
from forgepilot.runtime.guards import UsageTracker
from forgepilot.runtime.loop.turn import DEFAULT_MAX_STEPS, run_turn
from forgepilot.seams.agent_loop import (
    AGENTS_SERVICE_NAME,
    SERVICE_NAME,
    Agent,
    AgentFactory,
    AgentLoop,
    AgentSpec,
    AgentState,
    TurnResult,
    UserInput,
)

__all__ = [
    "AgentFactoryRegistry",
    "ForgePilotAgent",
    "ForgePilotAgentLoop",
    "ForgePilotAgentLoopPlugin",
    "ForgePilotAgentFactory",
]


class AgentFactoryRegistry(PluginService):
    """Serves ``agents`` — the table of engines this deployment can run."""

    provide = AGENTS_SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._factories: dict[str, AgentFactory] = {}

    def register_factory(self, factory: AgentFactory) -> None:
        key = getattr(factory, "key", None)
        if not key:
            raise PluginContractViolation("an agent factory must declare a key")
        self._factories[str(key)] = factory

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def factory(self, key: str) -> AgentFactory:
        try:
            return self._factories[key]
        except KeyError:
            raise PluginContractViolation(
                f"no agent engine registered under {key!r}; available: {list(self.keys())}"
            ) from None

    def build(self, key: str, spec: AgentSpec, kernel_ctx: Any = None) -> AgentLoop:
        return self.factory(key).build(spec, kernel_ctx)


@dataclass
class ForgePilotAgent:
    """One agent instance, bound to one session and one set of constraints."""

    spec: AgentSpec
    session: Any
    llm: Any
    tools: Any
    workspace_root: Path | None = None
    system_sections: tuple[str, ...] = ()
    tool_schemas: tuple[Any, ...] = ()
    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int = 4096
    temperature: float = 0.0
    tracker: UsageTracker = field(default_factory=UsageTracker)
    _state: AgentState = AgentState.IDLE
    _cancel_reason: str | None = None

    @property
    def spec_(self) -> AgentSpec:  # pragma: no cover - alias
        return self.spec

    def state(self) -> AgentState:
        return self._state

    def cancel(self, reason: str = "user") -> None:
        """Ask the turn to stop at the next step boundary.

        Deliberately cooperative: killing a turn mid-tool would leave the log with
        a call and no result, and the recovery sweep would have to invent one. A
        boundary stop keeps the log honest about what actually ran.
        """
        self._cancel_reason = reason
        if self._state is AgentState.RUNNING:
            self._state = AgentState.CANCELLING

    def _is_cancelled(self) -> bool:
        return self._cancel_reason is not None

    async def run_turn(self, user_input: UserInput) -> TurnResult:
        if self._state is AgentState.RUNNING:
            raise RuntimeError(
                f"agent {self.spec.agent_id!r} is already running a turn. One session has "
                f"one runner; overlapping turns would interleave in the log."
            )

        self._state = AgentState.RUNNING
        try:
            result = await run_turn(
                session=self.session,
                user_input=user_input,
                llm=self.llm,
                tools=self.tools,
                constraints=self.spec.constraints,
                model=getattr(self.llm, "model", "unknown"),
                system_sections=self.system_sections or self.spec.system_sections,
                workspace_root=self.workspace_root,
                tool_schemas=self.tool_schemas,
                max_steps=self.max_steps,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                tracker=self.tracker,
                is_cancelled=self._is_cancelled,
            )
            self._state = AgentState.DONE
            return result
        except BaseException:
            self._state = AgentState.FAILED
            raise
        finally:
            self._cancel_reason = None


@dataclass
class ForgePilotAgentLoop:
    """The built-in engine.

    Services are resolved from ``kernel_ctx`` at :meth:`create` time rather than
    at construction, so the loop is built against whichever context — normally a
    request scope — the caller means.
    """

    kernel_ctx: Any
    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int = 4096
    temperature: float = 0.0
    workspace_root: Path | None = None

    def create(self, spec: AgentSpec) -> Agent:
        ctx = self.kernel_ctx
        tools = ctx.tools
        return ForgePilotAgent(
            spec=spec,
            session=_session_for(ctx, spec),
            llm=ctx.llm,
            tools=tools,
            workspace_root=self.workspace_root,
            tool_schemas=tools.schemas(),
            max_steps=self.max_steps,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )


def _session_for(ctx: Any, spec: AgentSpec) -> Any:
    """The session this agent writes to.

    Supplied by the caller as ``ctx.session`` — the runtime session object, which
    is distinct from the ``sessionPersistence`` service it stores through.
    """
    session = getattr(ctx, "session", None)
    if session is None:
        raise PluginContractViolation(
            f"no session is available on this context, so agent {spec.agent_id!r} would "
            f"have nowhere to record what the model saw. The loop cannot run without one."
        )
    return session


class ForgePilotAgentFactory:
    """The factory registered under the key ``forgepilot``."""

    key = "forgepilot"

    def __init__(self, *, max_steps: int = DEFAULT_MAX_STEPS, max_tokens: int = 4096,
                 temperature: float = 0.0, workspace_root: Path | None = None) -> None:
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._workspace_root = workspace_root

    def build(self, spec: AgentSpec, kernel_ctx: Any) -> AgentLoop:
        return ForgePilotAgentLoop(
            kernel_ctx=kernel_ctx,
            max_steps=self._max_steps,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            workspace_root=self._workspace_root,
        )


class ForgePilotAgentLoopPlugin(PluginService):
    """Serves ``agentLoop`` and registers the built-in engine on ``agents``."""

    provide = SERVICE_NAME
    inject = ["agents"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self.engine_key: str = str(cfg.get("engine", "forgepilot"))
        self._factory = ForgePilotAgentFactory(
            max_steps=int(cfg.get("max_steps", DEFAULT_MAX_STEPS)),
            max_tokens=int(cfg.get("max_tokens", 4096)),
            temperature=float(cfg.get("temperature", 0.0)),
        )
        ctx.agents.register_factory(self._factory)

    def create(self, spec: AgentSpec) -> Agent:
        return self._factory.build(spec, self.ctx).create(spec)
