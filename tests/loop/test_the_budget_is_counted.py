"""Budgets are **counted**, not merely checked.

⚠️ **The defect this exists to catch, and why the existing tests could not.**

Every budget test in this suite *seeded* the tracker — `tracker.tool_calls =
10_000` — and then asserted the guard refused. That proves the guard reads the
field. It does not prove anything ever writes it, and for a long time nothing did
on the path a person actually uses:

- `ForgePilotAgent` defaults `tracker` to `field(default_factory=UsageTracker)`.
  `ForgePilotAgentLoop.create()` never passed one, so **the loop counted into a
  private tracker the agent owned alone**, and `turn.py` incremented that.
- `open_request_scope` builds the budget guard around the **scope's** tracker.
- So the guard watched a tracker nothing incremented. On the chat and HTTP paths —
  both of which go through `create()` — `max_tool_calls` and `max_tokens` were
  dead. `cost_usd` and `wall_seconds` were dead *everywhere*, because no code
  wrote them at all, on any path.

`run_task` wires the tracker by hand, which is the only reason task mode worked —
and that is exactly why the defect was invisible: the one path with a test that
drove a whole task was the one path that was correct.

**Every other rig in this suite hand-builds the tracker and the guards together**,
which is the single arrangement in which this cannot appear. So these tests build
the scope the way production does — `assemble_session`, then
`root.agents.build("forgepilot", spec, runtime.scope.ctx)` — and assert on what a
person's run would see.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgepilot.seams.refs import AgentId, PrincipalId, SessionId, TenantId
from forgepilot.seams.security import (
    Budget,
    ExecutionConstraints,
    NetworkAccess,
    Operation,
    Principal,
)

PRINCIPAL = Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme"))


def constraints(budget: Budget | None = None) -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        PRINCIPAL,
        permitted_operations=frozenset(Operation),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
        budget=budget,
    )


async def build(tmp_path: Path, *, budget: Budget | None = None):
    """A root assembled the way `--chat` and the HTTP layer assemble one."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root
    from forgepilot.runtime.assembly import assemble_session

    root = create_root()
    fibers = []

    async def load(spec: str, config: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), config or {})
        fibers.append(fiber)
        return fiber

    for spec, config in [
        ("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin", None),
        (
            "forgepilot.providers.sandbox.recording:RecordingSandboxPlugin",
            {"enforcement": "full"},
        ),
        ("forgepilot.runtime.tools.registry:ToolRegistryPlugin", None),
        ("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "ask"}),
        # A session cannot be opened without a decision authority, and `allow` is
        # right here for the same reason it is right in the shipped profile: the
        # *grant* is what denies, not this service.
        ("forgepilot.identity.policy:PolicyServicePlugin", {"default": "allow"}),
        ("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin", None),
        ("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-1"}),
        ("forgepilot.runtime.loop.agent_loop:AgentFactoryRegistry", None),
        ("forgepilot.runtime.loop.agent_loop:ForgePilotAgentLoopPlugin", None),
    ]:
        await load(spec, config)

    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    runtime = await assemble_session(
        root,
        constraints(budget),
        workspace_root=workspace,
        principal=PRINCIPAL,
    )
    return root, runtime, fibers


async def close(fibers) -> None:
    from forgepilot.kernel import dispose_fiber

    for fiber in reversed(fibers):
        await dispose_fiber(fiber)


def spec_for(constraints_):
    from forgepilot.seams.agent_loop import AgentSpec
    from forgepilot.seams.refs import Mode

    return AgentSpec(
        agent_id=AgentId("agent-1"),
        session_id=SessionId("budget-session"),
        mode=Mode("chat"),
        constraints=constraints_,
        system_sections=("you are an enterprise coding assistant",),
    )


async def agent_for(root, runtime, constraints_):
    spec = spec_for(constraints_)
    return root.agents.build("forgepilot", spec, runtime.scope.ctx).create(spec)


def script_tool_calls(root, count: int) -> None:
    from forgepilot.providers.llm.replay import text_turn, tool_call_turn

    for index in range(count):
        root.llm.script(
            tool_call_turn("read_file", json.dumps({"path": "src/app.py"}), call_id=f"c{index}")
        )
    root.llm.script(text_turn("done"))


# ---------------------------------------------------------------------------
# The wiring: one tracker, shared
# ---------------------------------------------------------------------------
async def test_the_agent_counts_into_the_tracker_its_guards_read(tmp_path: Path) -> None:
    """Identity, not equality. Two empty trackers compare equal and behave worlds
    apart: one is watched by the guard, the other is the one being filled.
    """
    root, runtime, fibers = await build(tmp_path)
    try:
        agent = await agent_for(root, runtime, constraints())
        assert agent.tracker is runtime.scope.tracker, (
            "the agent is counting into a tracker of its own, so the budget guard "
            "is watching one that nothing increments"
        )
    finally:
        await runtime.close()
        await close(fibers)


# ---------------------------------------------------------------------------
# The counting: without anyone seeding it
# ---------------------------------------------------------------------------
async def test_the_budget_guard_sees_what_the_loop_counted(tmp_path: Path) -> None:
    """⚠️ **Two halves enforce a budget, and only one of them was broken.**

    The damage was narrower than "budgets do nothing", and the difference matters:

    - `turn.py` checks the budget **between steps**, against the tracker passed to
      `run_turn` — which on the agent path is the agent's own. That half worked, so
      turns still stopped.
    - the **guard** checks it **per tool call**, against the scope's tracker. It
      never fired, because nothing incremented that one on this path.

    And the guard is the half the code calls non-decorative — *"a guard rather than
    a hook because exceeding a budget must be final"*. So this asserts the guard,
    with a count that came from the loop rather than from this test.
    """
    from forgepilot.seams.agent_loop import UserInput
    from forgepilot.seams.refs import TurnId
    from forgepilot.seams.tools import ToolCall

    budget = Budget(max_tool_calls=2)
    root, runtime, fibers = await build(tmp_path, budget=budget)
    try:
        script_tool_calls(root, 4)
        agent = await agent_for(root, runtime, constraints(budget))
        await agent.run_turn(UserInput(text="read the file"))

        assert runtime.scope.tracker.tool_calls >= 2, (
            "the loop counted nothing into the tracker its guards read"
        )

        outcome = await runtime.pipeline.invoke(
            ToolCall(call_id="c9", name="read_file", arguments_json='{"path": "src/app.py"}'),
            session=runtime.session,
            turn=TurnId(1),
            step=9,
            constraints=constraints(budget),
        )

        assert outcome.meta.get("guard") == "budget", (
            f"a call made after the budget was spent was not refused by the budget "
            f"guard: {outcome.meta}"
        )
    finally:
        await runtime.close()
        await close(fibers)


async def test_tokens_and_wall_clock_accumulate(tmp_path: Path) -> None:
    """`wall_seconds` had no writer at all. Tokens had one, but not on this path.

    The usage is scripted rather than produced, because the replay model reports
    none — a real endpoint sends a `usage` chunk with `stream_options.include_usage`,
    and `text_turn(usage=...)` is the seam's own way of standing in for that.
    """
    from forgepilot.providers.llm.replay import text_turn
    from forgepilot.seams.agent_loop import UserInput
    from forgepilot.seams.llm import Usage

    root, runtime, fibers = await build(tmp_path)
    try:
        from forgepilot.providers.llm.replay import tool_call_turn

        for index in range(2):
            root.llm.script(
                tool_call_turn(
                    "read_file",
                    json.dumps({"path": "src/app.py"}),
                    call_id=f"c{index}",
                    usage=Usage(input_tokens=120, output_tokens=30),
                )
            )
        root.llm.script(text_turn("done"))

        agent = await agent_for(root, runtime, constraints())
        await agent.run_turn(UserInput(text="read the file"))

        tracker = runtime.scope.tracker
        assert tracker.tool_calls >= 2, tracker
        assert tracker.tokens == 300, (
            f"counted {tracker.tokens} token(s), so max_tokens cannot fire"
        )
        assert tracker.wall_seconds > 0, (
            "no wall-clock time was counted, so max_wall_seconds cannot fire"
        )
    finally:
        await runtime.close()
        await close(fibers)


# ---------------------------------------------------------------------------
# Cost — the dimension the model has to price
# ---------------------------------------------------------------------------
async def test_the_loop_adds_what_the_model_charged(tmp_path: Path) -> None:
    """Cost reached the tracker nowhere at all, on any path.

    `usage.cost_usd` is the provider's to fill in (`openai_compat.apply_price`),
    so what is under test here is the *loop* half: that whatever the model says it
    charged lands on the tracker the guard reads.
    """
    from forgepilot.providers.llm.replay import text_turn
    from forgepilot.seams.agent_loop import UserInput
    from forgepilot.seams.llm import Usage

    root, runtime, fibers = await build(tmp_path)
    try:
        root.llm.script(text_turn("nothing to do", usage=Usage(input_tokens=1, cost_usd=0.25)))

        agent = await agent_for(root, runtime, constraints())
        await agent.run_turn(UserInput(text="hello"))

        assert runtime.scope.tracker.cost_usd == pytest.approx(0.25)
    finally:
        await runtime.close()
        await close(fibers)


async def test_a_cost_budget_stops_the_loop(tmp_path: Path) -> None:
    """Tool calls rather than text: a text turn *ends* the turn, so a budget that
    is only checked at the top of the next step would never be reached.
    """
    from forgepilot.providers.llm.replay import tool_call_turn
    from forgepilot.seams.agent_loop import UserInput
    from forgepilot.seams.llm import Usage

    budget = Budget(max_cost_usd=0.10)
    root, runtime, fibers = await build(tmp_path, budget=budget)
    try:
        for index in range(5):
            root.llm.script(
                tool_call_turn(
                    "read_file",
                    json.dumps({"path": "src/app.py"}),
                    call_id=f"c{index}",
                    usage=Usage(input_tokens=1, cost_usd=0.25),
                )
            )

        agent = await agent_for(root, runtime, constraints(budget))
        result = await agent.run_turn(UserInput(text="keep going"))

        assert result.reason == "budget", (
            f"a step costing $0.25 did not exhaust a $0.10 budget ({result.reason!r})"
        )
    finally:
        await runtime.close()
        await close(fibers)
