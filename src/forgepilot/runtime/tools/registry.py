"""The tool registry as a service — name ``tools``.

The registry is the **catalog**: it knows which tools exist, what they declare,
and what schema the model should be shown. Execution is deliberately not its job
beyond delegation — ``invoke`` forwards to a
:class:`~forgepilot.runtime.tools.pipeline.ToolPipeline`, which is the only code
that ever calls a tool body.

It is a Cordis service, so registration is scoped: different sessions and
tenants can see different tool sets. That matters because **which tools are
visible is itself part of the permission story** — a session that cannot open a
pull request has no reason to be shown the tool that does.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.kernel.errors import PluginContractViolation
from forgepilot.seams.llm import ToolSchema
from forgepilot.seams.refs import TurnId
from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.tools import (
    SERVICE_NAME,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolOutcome,
    ToolSessionRecorder,
)

__all__ = ["ToolRegistryPlugin"]


class ToolRegistryPlugin(PluginService):
    """Serves ``tools``."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._tools: dict[str, ToolDefinition] = {}
        self._pipeline: Any = None
        self._order: list[str] = []

    # -- wiring ------------------------------------------------------------
    def attach_pipeline(self, pipeline: Any) -> None:
        """Bind the executor. Kept explicit so there is exactly one per registry."""
        self._pipeline = pipeline

    # -- catalog -----------------------------------------------------------
    def register(self, definition: ToolDefinition) -> Any:
        if not definition.name:
            raise PluginContractViolation("a tool must have a name")
        if not definition.effects:
            # A tool that declares no effects cannot be granted any capability,
            # so it would be registered and then always fail. Refusing here makes
            # the omission visible at registration instead of at first call.
            raise PluginContractViolation(
                f"tool {definition.name!r} declares no effects. Declare at least one "
                f"({', '.join(e.value for e in ToolEffect)}) so the pipeline knows "
                f"what it must be granted."
            )
        if definition.name not in self._tools:
            self._order.append(definition.name)
        self._tools[definition.name] = definition

        def unregister() -> None:
            self._tools.pop(definition.name, None)
            if definition.name in self._order:
                self._order.remove(definition.name)

        return unregister

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def definitions(self) -> Sequence[ToolDefinition]:
        return tuple(self._tools[name] for name in self._order)

    def schemas(self) -> tuple[ToolSchema, ...]:
        return tuple(
            ToolSchema(name=d.name, description=d.description, parameters=dict(d.parameters))
            for d in self.definitions()
        )

    def names(self) -> tuple[str, ...]:
        return tuple(self._order)

    # -- execution ---------------------------------------------------------
    async def invoke(
        self,
        call: ToolCall,
        *,
        recorder: ToolSessionRecorder,
        turn: TurnId,
        step: int,
        constraints: ExecutionConstraints,
        workspace_root: Path | None = None,
    ) -> ToolOutcome:
        if self._pipeline is None:
            raise PluginContractViolation(
                "no pipeline is attached, so tool execution has no approval, no guards "
                "and no sandbox. Refusing rather than running a tool bare."
            )
        return await self._pipeline.invoke(
            call,
            session=recorder,
            turn=turn,
            step=step,
            constraints=constraints,
            workspace_root=workspace_root,
        )
