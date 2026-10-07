"""A turn: claim input, run steps until nothing is owed, close.

The loop terminates on one of a closed set of reasons, and every one of them is
recorded. "The agent stopped" is not an answer anyone can act on; "the agent
stopped because the tool-call budget ran out" is.

A hard step ceiling exists even though the budget would usually bite first: the
two protect against different things. The budget bounds *cost*, the ceiling bounds
*looping* — a model that keeps calling a cheap tool can burn a lot of wall-clock
inside a budget that looks healthy.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from forgepilot.runtime.loop.step import ContextWindowExceeded, StepResult, run_step
from forgepilot.seams.agent_loop import TurnResult, UserInput
from forgepilot.seams.refs import TurnId
from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.session import TURN_END_REASONS

__all__ = ["run_turn", "ContextWindowExceeded", "DEFAULT_MAX_STEPS"]

#: Steps per turn. Bounds looping rather than cost — see the module docstring.
DEFAULT_MAX_STEPS = 12


async def run_turn(
    *,
    session: Any,
    user_input: UserInput,
    llm: Any,
    tools: Any,
    constraints: ExecutionConstraints,
    model: str,
    system_sections: tuple[str, ...] = (),
    workspace_root: Path | None = None,
    tool_schemas: tuple[Any, ...] = (),
    max_steps: int = DEFAULT_MAX_STEPS,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    tracker: Any = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> TurnResult:
    """Run one turn to completion, always closing it in the log."""
    turn = session.next_turn()
    await session.turn_start(turn)
    await session.user_message(turn, user_input.text)

    reason = "completed"
    text_parts: list[str] = []
    tool_calls = 0

    try:
        for step in range(max_steps):
            if is_cancelled is not None and is_cancelled():
                reason = "cancelled"
                break

            if tracker is not None:
                limits = constraints.budget
                if (
                    tracker.tool_calls >= limits.max_tool_calls
                    or tracker.tokens >= limits.max_tokens
                    or tracker.cost_usd >= limits.max_cost_usd
                    or tracker.wall_seconds >= limits.max_wall_seconds
                ):
                    reason = "budget"
                    break

            if step == max_steps - 1:
                # The ceiling is checked *before* the step that would exceed it,
                # so a turn that hits it ends with the reason saying so rather
                # than looking like a model that simply stopped.
                reason = "error"
                break

            result: StepResult = await run_step(
                session=session,
                llm=llm,
                tools=tools,
                constraints=constraints,
                turn=turn,
                step=step,
                workspace_root=workspace_root,
                model=model,
                system_sections=system_sections,
                max_tokens=max_tokens,
                temperature=temperature,
                tool_schemas=tuple(tool_schemas),
            )

            if result.text:
                text_parts.append(result.text)
            tool_calls += len(result.tool_calls)

            if tracker is not None:
                tracker.tokens += result.usage.input_tokens + result.usage.output_tokens
                tracker.tool_calls += len(result.tool_calls)

            if not result.wants_tools:
                break

    except ContextWindowExceeded:
        # Closed as an error rather than left open: the projection must always
        # see balanced turns, and an unbalanced one reads as a crash.
        reason = "error"
        raise
    except BaseException:
        reason = "error"
        raise
    finally:
        if reason not in TURN_END_REASONS:
            reason = "error"
        await session.turn_end(turn, reason)
        await session.flush()

    return TurnResult(
        turn=TurnId(turn),
        reason=reason,
        assistant_text="\n".join(text_parts),
        tool_calls=tool_calls,
    )
