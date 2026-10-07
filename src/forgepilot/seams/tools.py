"""Tool seam — service name ``tools``.

Two structural rules live in this module.

**A tool never constructs its own context.** :class:`ToolContext` is built by the
tool pipeline and handed to the tool, frozen. A tool therefore cannot widen its
own authority, and it cannot reach for the root context — which matters because
the root context reads services *unchecked* and silently returns ``None`` for
anything missing (see ``CLAUDE.md``). A tool holding a root context would inherit
that hole.

**Everything funnels through the pipeline.** ``invoke`` is the only entry point,
including for programmatic tool calling, so nothing can skip approval, guards or
the sandbox by calling a tool body directly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from forgepilot.seams.llm import ToolSchema
from forgepilot.seams.refs import SessionId
from forgepilot.seams.security import ExecutionConstraints

__all__ = [
    "SERVICE_NAME",
    "ToolEffect",
    "ToolCall",
    "ToolOutcome",
    "ToolContext",
    "ToolDefinition",
    "ToolRegistry",
]

SERVICE_NAME = "tools"


class ToolEffect(StrEnum):
    """What a tool does, and therefore what it must be granted before running.

    Declared by the tool and checked by the pipeline; a tool is only handed the
    capabilities its declared effects permit.
    """

    READ = "read"
    WRITE = "write"
    EXEC = "exec"
    NETWORK = "network"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    #: Raw, unparsed argument JSON as emitted by the model. Kept verbatim so a
    #: malformed-argument failure stays replayable from the session log.
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    content: str
    is_error: bool = False
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a tool is allowed to know. Built by the pipeline, not the tool.

    Note what is **not** here: no root context, no service container, no way to
    obtain a capability the tool's declared effects did not earn. Adding one
    would undo the separation between "permission decided" and "effect executed".
    """

    session_id: SessionId
    constraints: ExecutionConstraints
    workspace_root: Path | None = None
    #: Wall-clock ceiling for this one call, derived from the remaining budget.
    timeout_s: float = 120.0


ToolBody = Callable[[ToolCall, ToolContext], Awaitable[ToolOutcome]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]
    effects: frozenset[ToolEffect]
    execute: ToolBody
    #: Last word on the content string. Synchronous and content-only by design,
    #: so it cannot become a second place where side effects hide.
    finalize_content: Callable[[ToolOutcome], str] | None = None


@runtime_checkable
class ToolRegistry(Protocol):
    def register(self, definition: ToolDefinition) -> Any:
        """Register a tool. Returns a disposable that unregisters it."""
        ...

    def schemas(self) -> tuple[ToolSchema, ...]:
        """The tools visible in *this* scope, as the model should see them."""
        ...

    def get(self, name: str) -> ToolDefinition | None: ...

    async def invoke(self, call: ToolCall, context: ToolContext) -> ToolOutcome:
        """Run a tool through the full pipeline. The only entry point."""
        ...

    def definitions(self) -> Sequence[ToolDefinition]: ...
