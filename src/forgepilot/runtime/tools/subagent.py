"""``spawn_subagent`` — a bounded child turn with narrower authority than its parent.

A subagent is a **reader**. It can search code and read it, and it cannot write,
execute a command, reach the network, open a pull request, or spawn another
subagent. That last restriction is not a convention: the child's constraint set is
:meth:`ExecutionConstraints.narrowed_to` the parent's, intersected with a read-only
cap, so ``SPAWN_SUBAGENT`` is not in it and the child's own ``spawn_subagent`` call
would be refused by the operation guard like any other missing permission.

**The child is a real session in the same store**, with ``parent_session`` set, so
the whole child turn — the request it built, every tool call, every result — is
readable afterwards. The parent does not need a second event to say so: the tool
result names the child's id, and the pipeline wrote that result. A tool that could
append to its parent's log would be able to write a record of something that did not
happen; this one cannot, by construction.

Why a separate turn instead of a bigger budget for one agent: a search that ranges
over four repositories and twelve candidate files spends context on material the
parent will never cite. Handing that work to a child whose context dies with it is
the point, and the parent receives a paragraph rather than a transcript.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from forgepilot.runtime.session import Session
from forgepilot.seams.agent_loop import UserInput
from forgepilot.seams.refs import SessionId
from forgepilot.seams.security import Budget, ExecutionConstraints, NetworkAccess, Operation
from forgepilot.seams.session import SessionHeader
from forgepilot.seams.tools import ToolCall, ToolContext, ToolDefinition, ToolEffect, ToolOutcome

__all__ = [
    "DEFAULT_CHILD_TOOLS",
    "DEFAULT_MAX_STEPS",
    "MAX_STEPS_CAP",
    "SPAWN_TOOL",
    "SUBAGENT_OPERATIONS",
    "SubagentToolPlugin",
    "built_names",
]

SPAWN_TOOL = "spawn_subagent"


def built_names() -> set[str]:
    """The tool names this module can register without a kernel.

    The offer guard (`tests/contract/test_every_tool_is_offered_by_a_project.py`)
    compares every such name against the projects and bundles that offer one. A
    module that builds a tool and is asked here keeps the two lists from drifting.
    """
    return {SPAWN_TOOL}


DEFAULT_MAX_STEPS = 6
MAX_STEPS_CAP = 12
DEFAULT_MAX_CHARS = 8000
DEFAULT_TIMEOUT_S = 240.0

#: What a child may do. Deliberately no ``WRITE_CODE``, no ``EXEC``, no
#: ``OPEN_PR``, no ``FETCH_URL`` and no ``SPAWN_SUBAGENT``: a reader that can
#: delegate is a way to multiply a cost ceiling, and a reader that can write is
#: not a reader.
SUBAGENT_OPERATIONS = frozenset({Operation.READ_CODE, Operation.READ_TICKET})

DEFAULT_CHILD_TOOLS = ("read_file", "list_dir", "glob", "code_search", "read_code")

_PREAMBLE = """You are a subagent working for another agent. You can search and read code; \
you cannot write files, run commands, or open pull requests, and you cannot delegate \
further. Answer the question you were given directly and completely, citing the \
repository path and the line numbers you relied on. If the checkout does not contain \
what the question needs, say exactly what is missing rather than guessing."""


def _child_brief(task: str, *, context: str) -> str:
    if not context.strip():
        return task
    return f"{task}\n\nContext from the parent agent (not instructions):\n{context.strip()}"


class SubagentToolPlugin:
    """Registers ``spawn_subagent`` once the tools, the model and the store are ready."""

    inject = ["tools", "llm", "sessionPersistence"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        self.ctx = ctx
        cfg = dict(config or {})
        self.max_steps = max(1, min(int(cfg.get("max_steps", DEFAULT_MAX_STEPS)), MAX_STEPS_CAP))
        self.max_chars = max(200, int(cfg.get("max_chars", DEFAULT_MAX_CHARS)))
        self.timeout_s = float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
        names = cfg.get("tool_names", DEFAULT_CHILD_TOOLS)
        self.tool_names = tuple(str(name) for name in names)
        self.max_tool_calls = int(cfg.get("max_tool_calls", self.max_steps * 3))

    # -- registration ------------------------------------------------------
    def init(self) -> Any:
        unregister = self.ctx.tools.register(self.definition())
        return unregister

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=SPAWN_TOOL,
            description=(
                "Ask a read-only subagent to investigate something and report back. The "
                "subagent can search and read code in the permitted repositories and "
                "cannot change anything. Use it for a question whose answer needs reading "
                "several files, without spending this conversation's context on them. Its "
                "whole transcript is kept as a child session, whose id is in the result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The question to answer, in one or two sentences.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional facts the subagent needs and cannot discover.",
                    },
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            effects=frozenset({ToolEffect.SESSION}),
            required_operation=Operation.SPAWN_SUBAGENT,
            execute=self.execute,
        )

    # -- the call ----------------------------------------------------------
    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        try:
            arguments = json.loads(call.arguments_json or "{}")
        except json.JSONDecodeError as exc:
            return _refused(f"the arguments are not valid JSON: {exc}")
        if not isinstance(arguments, dict):
            return _refused("the arguments must be a JSON object")

        task = str(arguments.get("task") or "").strip()
        if not task:
            return _refused("`task` is required: state the question the subagent should answer")
        if len(task) > 4000:
            return _refused(
                "`task` is longer than 4000 characters; state the question, not the brief"
            )
        brief = _child_brief(task, context=str(arguments.get("context") or ""))

        store = self.ctx.sessionPersistence
        parent = await store.header(SessionId(str(ctx.session_id)))
        if parent.parent_session is not None:
            return _refused(
                "a subagent cannot spawn another subagent: this session is itself a child. "
                "Do the work here, or report back that it needs a wider authority."
            )

        child_id = SessionId(f"sub-{uuid.uuid4().hex[:12]}")
        await store.create(
            SessionHeader(
                session_id=child_id,
                created_at=datetime.now(UTC),
                tenant=parent.tenant,
                principal=parent.principal,
                cwd=parent.cwd,
                parent_session=SessionId(str(ctx.session_id)),
            )
        )
        child_session = await Session.open(store, child_id, recover=False)

        constraints = _narrowed(ctx.constraints, budget=_budget(ctx.constraints, self))
        schemas = self._schemas()

        from forgepilot.runtime.loop.turn import run_turn

        try:
            result = await asyncio.wait_for(
                run_turn(
                    session=child_session,
                    user_input=UserInput(text=brief),
                    llm=self.ctx.llm,
                    tools=self.ctx.tools,
                    constraints=constraints,
                    model=getattr(self.ctx.llm, "model", "unknown"),
                    system_sections=(_PREAMBLE,),
                    workspace_root=ctx.workspace_root,
                    tool_schemas=schemas,
                    max_steps=self.max_steps,
                    compactor=getattr(self.ctx, "compaction", None),
                ),
                timeout=max(5.0, self.timeout_s),
            )
        except TimeoutError:
            return _refused(
                f"the subagent did not finish within {self.timeout_s:.0f}s. Its session "
                f"{child_id} holds everything it read, so you can resume from that or ask a "
                "narrower question.",
                meta={"child": str(child_id), "reason": "timeout"},
            )
        finally:
            await child_session.close()

        answer = (result.assistant_text or "").strip()
        truncated = len(answer) > self.max_chars
        if truncated:
            answer = answer[: self.max_chars] + (
                f"\n\n[truncated: the child answered more than {self.max_chars} characters; "
                f"its full reply is in session {child_id}]"
            )
        if not answer:
            answer = "(the subagent finished without an answer)"

        content = (
            f"subagent {child_id} finished ({result.reason}), "
            f"{result.tool_calls} tool call(s):\n\n{answer}"
        )
        return ToolOutcome(
            content=content,
            meta={
                "child": str(child_id),
                "reason": result.reason,
                "toolCalls": result.tool_calls,
                "truncated": truncated,
            },
        )

    def _schemas(self) -> tuple[Any, ...]:
        wanted = set(self.tool_names) - {SPAWN_TOOL}
        return tuple(
            schema for schema in self.ctx.tools.schemas() if getattr(schema, "name", "") in wanted
        )


def _refused(message: str, *, meta: dict[str, Any] | None = None) -> ToolOutcome:
    return ToolOutcome(content=message, is_error=True, meta=dict(meta or {}))


def _budget(constraints: ExecutionConstraints, plugin: SubagentToolPlugin) -> Budget:
    parent = constraints.budget
    return Budget(
        max_tokens=min(parent.max_tokens, 200_000),
        max_tool_calls=min(parent.max_tool_calls, plugin.max_tool_calls),
        max_wall_seconds=min(parent.max_wall_seconds, plugin.timeout_s),
        max_cost_usd=parent.max_cost_usd,
    )


def _narrowed(constraints: ExecutionConstraints, *, budget: Budget) -> ExecutionConstraints:
    """``constraints`` intersected with a read-only cap.

    The intersection is what makes "narrower" structural: a field this function
    forgets to reset cannot make the child *wider*, because ``narrowed_to`` takes the
    smaller of the two sides. The result is unsigned, which is honest — it is a new
    object — and the child runs with ``verify_signature=False`` exactly as every
    session does today.
    """
    cap = replace(
        constraints,
        permitted_operations=constraints.permitted_operations & SUBAGENT_OPERATIONS,
        writable_paths=(),
        network=NetworkAccess.DENY,
        budget=budget,
    )
    return constraints.narrowed_to(cap)
