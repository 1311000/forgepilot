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

import asyncio
from collections.abc import Mapping, Sequence
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

    async def invoke_all(
        self,
        calls: Sequence[ToolCall],
        *,
        recorder: ToolSessionRecorder,
        turn: TurnId,
        step: int,
        constraints: ExecutionConstraints,
        workspace_root: Path | None = None,
    ) -> None:
        """Run the calls a model asked for in one step, overlapping the safe ones.

        A mature agent asks for several reads at once, and paying their latency
        one after another is a self-inflicted cost: they cannot observe each
        other. Writes and execs can, so they are their own group — the ordering
        rule is the *declared* effect, not a guess about the arguments, because
        the declaration is the only thing the registry actually knows.

        The groups are consecutive: a read after a write still waits for the
        write, so "read the file I just changed" keeps meaning what it means in
        a sequential step. Only a run of adjacent read-only calls overlaps.

        Two consequences worth stating rather than discovering:

        - **Each call records its own ``tool/call`` before it runs** (the
          pipeline's durable-before-execution rule is untouched). Calls therefore
          appear in request order, while results appear as they finish. The log
          is a record, not a schedule, and results are paired by ``callId``.
        - **Each call gets its own pipeline clone**, because the pipeline keeps
          the phase trace of "the most recent call" on itself. Sharing one would
          make that trace a race rather than a diagnostic.
        """
        if self._pipeline is None:
            raise PluginContractViolation(
                "no pipeline is attached, so tool execution has no approval, no guards "
                "and no sandbox. Refusing rather than running a tool bare."
            )

        for parallel, group in _groups(calls, self._tools):
            if parallel and len(group) > 1:
                await asyncio.gather(
                    *(
                        self._pipeline.clone().invoke(
                            call,
                            session=recorder,
                            turn=turn,
                            step=step,
                            constraints=constraints,
                            workspace_root=workspace_root,
                        )
                        for call in group
                    )
                )
            else:
                for call in group:
                    await self.invoke(
                        call,
                        recorder=recorder,
                        turn=turn,
                        step=step,
                        constraints=constraints,
                        workspace_root=workspace_root,
                    )


#: Effects that let a call share a step with its neighbours. A reader and a
#: session-only tool change nothing another call can observe; a writer, an
#: executor or a network call changes what the next call would see, so it runs
#: alone and in order.
PARALLEL_EFFECTS = frozenset({ToolEffect.READ, ToolEffect.SESSION})


def _may_share_a_step(definition: ToolDefinition | None) -> bool:
    # An unknown tool is *not* parallel-safe: it will be refused, and a refusal
    # belongs in request order rather than in a burst with its neighbours.
    return definition is not None and definition.effects <= PARALLEL_EFFECTS


def _groups(
    calls: Sequence[ToolCall], definitions: Mapping[str, ToolDefinition]
) -> list[tuple[bool, list[ToolCall]]]:
    """Split a step's calls into runs that may overlap.

    Consecutive is the point: `[read, read, write, read]` is three groups, so the
    trailing read still sees the write. Grouping all the reads together would
    reorder the step and make the second read a snapshot of a tree that has not
    been written yet.
    """
    groups: list[tuple[bool, list[ToolCall]]] = []
    for call in calls:
        safe = _may_share_a_step(definitions.get(call.name))
        if safe and groups and groups[-1][0]:
            groups[-1][1].append(call)
        else:
            groups.append((safe, [call]))
    return groups
