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
from forgepilot.seams.refs import SessionId, TurnId
from forgepilot.seams.security import ExecutionConstraints

__all__ = [
    "SERVICE_NAME",
    "ToolEffect",
    "ToolCall",
    "ToolOutcome",
    "Capabilities",
    "ResourceTargets",
    "ToolContext",
    "ToolDefinition",
    "ToolRegistry",
    "ToolSessionRecorder",
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
class Capabilities:
    """What this particular invocation was actually granted.

    Derived from the tool's declared :class:`ToolEffect` set **and** the caller's
    constraints **and** the host's sandbox enforcement — the intersection, not
    any one of them. A tool whose effects claim ``WRITE`` still gets
    ``fs_write=False`` if the constraints or the sandbox refuse it.

    Tools must check these rather than assuming their declaration was honoured:
    the declaration is what the tool *asks* for, this is what it *got*.
    """

    fs_read: bool = False
    fs_write: bool = False
    exec: bool = False
    network: bool = False

    @classmethod
    def for_effects(cls, effects: frozenset[ToolEffect]) -> Capabilities:
        return cls(
            fs_read=ToolEffect.READ in effects,
            fs_write=ToolEffect.WRITE in effects,
            exec=ToolEffect.EXEC in effects,
            network=ToolEffect.NETWORK in effects,
        )

    def intersect(self, other: Capabilities) -> Capabilities:
        """Narrow to what both permit. Capabilities only ever shrink."""
        return Capabilities(
            fs_read=self.fs_read and other.fs_read,
            fs_write=self.fs_write and other.fs_write,
            exec=self.exec and other.exec,
            network=self.network and other.network,
        )

    def without(self, *names: str) -> Capabilities:
        """Revoke named capabilities. Used to apply the sandbox's verdict."""
        values = {
            "fs_read": self.fs_read,
            "fs_write": self.fs_write,
            "exec": self.exec,
            "network": self.network,
        }
        for name in names:
            if name not in values:
                raise ValueError(f"unknown capability {name!r}")
            values[name] = False
        return Capabilities(**values)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a tool is allowed to know. Built by the pipeline, not the tool.

    Note what is **not** here: no root context, no service container, no session
    handle, and no way to obtain a capability the tool's declared effects did not
    earn. Adding any of them would undo the separation between "permission was
    decided" and "an effect was executed".

    In particular a tool cannot record its own events — the pipeline does all
    logging, so a tool has no way to write a result different from the one that
    actually happened.
    """

    session_id: SessionId
    constraints: ExecutionConstraints
    turn: TurnId
    step: int
    capabilities: Capabilities = field(default_factory=Capabilities)
    workspace_root: Path | None = None
    #: Wall-clock ceiling for this one call, derived from the remaining budget.
    timeout_s: float = 120.0


ToolBody = Callable[[ToolCall, ToolContext], Awaitable[ToolOutcome]]


@dataclass(frozen=True, slots=True)
class ResourceTargets:
    """What a call would touch, in normalised form.

    Extracted by the tool definition rather than by each guard, so guards never
    parse a tool's argument JSON. One place knows that ``{"file": ...}`` means a
    path; every guard can then reason about ``paths`` without caring which tool
    produced them.
    """

    repos: tuple[Any, ...] = ()
    #: Workspace-relative paths. Absolute paths and ``..`` escapes are the
    #: guard's problem, not the extractor's.
    paths: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]
    effects: frozenset[ToolEffect]
    execute: ToolBody
    #: The permission this tool needs, in the vocabulary policy speaks.
    #:
    #: Declared rather than derived from ``effects``: effects say what the call
    #: would *do*, this says what it must be *permitted* to do, and the two are
    #: not the same question. Two tools can both write files while only one of
    #: them is "opening a pull request". ``None`` means the tool needs no
    #: operation — appropriate only for genuinely inert tools.
    required_operation: Any = None
    #: Pure extractor: arguments in, targets out. Must not perform I/O — it runs
    #: in the pre-execute phase, which is required to be side-effect free.
    describe_targets: Callable[[Mapping[str, Any]], ResourceTargets] | None = None
    #: Last word on the content string. Synchronous and content-only by design,
    #: so it cannot become a second place where side effects hide.
    finalize_content: Callable[[ToolOutcome], str] | None = None


@runtime_checkable
class ToolSessionRecorder(Protocol):
    """The narrow slice of a session the tool pipeline writes through.

    Deliberately not the whole ``Session``: the pipeline needs to *append* tool
    events, not to read history or to append anything else. Handing it a narrow
    recorder keeps the pipeline's log access auditable at a glance — every write
    it can make is named here.

    ``Session`` satisfies this structurally.
    """

    @property
    def session_id(self) -> SessionId: ...

    async def tool_call(
        self, turn: TurnId, step: int, *, call_id: str, name: str, arguments_json: str
    ) -> Any: ...

    async def tool_result(
        self,
        turn: TurnId,
        step: int,
        *,
        call_id: str,
        content: str,
        is_error: bool,
        meta: Mapping[str, Any] | None,
    ) -> Any: ...

    async def append(
        self,
        kind: str,
        data: Mapping[str, Any] | None = None,
        *,
        surface: Any = None,
        turn: TurnId | None = None,
        step: int | None = None,
    ) -> Any: ...


@runtime_checkable
class ToolRegistry(Protocol):
    def register(self, definition: ToolDefinition) -> Any:
        """Register a tool. Returns a disposable that unregisters it."""
        ...

    def schemas(self) -> tuple[ToolSchema, ...]:
        """The tools visible in *this* scope, as the model should see them."""
        ...

    def get(self, name: str) -> ToolDefinition | None: ...

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
        """Run a tool through the full pipeline. **The only entry point.**

        Including programmatic tool calling: nothing may reach a tool body
        without passing approval, the guards and the sandbox.
        """
        ...

    def definitions(self) -> Sequence[ToolDefinition]: ...
