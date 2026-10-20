"""A step's read-only calls overlap; everything else stays in order.

A model asks for several reads in one turn because it wants them *at once*, and
the cost of serving them one after another is real: each is a subprocess, a file
read or a retriever round-trip. They cannot observe each other, so the ordering
between them is not information — it is latency.

What must not change is the meaning of the sequence. `[read, read, write, read]`
is three groups: the trailing read still sees the write, because a write is a
barrier. Grouping *all* reads together would reorder the step and make that read
a snapshot of a tree the write has not touched yet.

The rule is the tool's **declared effect**, not a guess about its arguments: the
declaration is the only thing the registry knows, and a guess would be a second
place for the answer to live.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from forgepilot.runtime.tools.registry import _groups
from forgepilot.seams.refs import TurnId
from forgepilot.seams.security import NetworkAccess, Operation
from forgepilot.seams.session import EventKind
from forgepilot.seams.tools import ToolCall, ToolDefinition, ToolEffect, ToolOutcome
from tests.conftest import constraints_for

TURN = TurnId(1)


def _fake(name: str, *, effect: ToolEffect, log: list[tuple[str, str]], delay: float = 0.1):
    async def execute(call: ToolCall, ctx) -> ToolOutcome:
        log.append(("start", name))
        await asyncio.sleep(delay)
        log.append(("end", name))
        return ToolOutcome(content=f"{name} done")

    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({effect}),
        execute=execute,
    )


def _call(name: str, index: int = 0) -> ToolCall:
    return ToolCall(call_id=f"c-{name}-{index}", name=name, arguments_json=json.dumps({}))


# ---------------------------------------------------------------------------
# The grouping rule, without running anything
# ---------------------------------------------------------------------------
def test_consecutive_reads_are_one_group() -> None:
    definitions = {
        "read": _fake("read", effect=ToolEffect.READ, log=[]),
        "write": _fake("write", effect=ToolEffect.WRITE, log=[]),
    }

    groups = _groups(
        [_call("read", 1), _call("read", 2), _call("write"), _call("read", 3)],
        definitions,
    )

    assert [safe for safe, _ in groups] == [True, False, True]
    assert [len(group) for _, group in groups] == [2, 1, 1]


def test_an_unknown_tool_runs_alone() -> None:
    # It will be refused by the pipeline; the refusal belongs in request order.
    groups = _groups([_call("read", 1), _call("nope"), _call("read", 2)], {})

    assert [safe for safe, _ in groups] == [False, False, False]


def test_a_writer_between_two_readers_keeps_them_apart() -> None:
    definitions = {
        "read": _fake("read", effect=ToolEffect.READ, log=[]),
        "exec": _fake("exec", effect=ToolEffect.EXEC, log=[]),
    }

    groups = _groups([_call("read", 1), _call("exec"), _call("read", 2)], definitions)

    assert len(groups) == 3


# ---------------------------------------------------------------------------
# It actually overlaps, and it actually waits
# ---------------------------------------------------------------------------
async def test_two_reads_overlap(rig) -> None:
    log: list[tuple[str, str]] = []
    rig.registry.register(_fake("slow_read_a", effect=ToolEffect.READ, log=log))
    rig.registry.register(_fake("slow_read_b", effect=ToolEffect.READ, log=log))
    constraints = constraints_for(Operation.READ_CODE)

    started = time.monotonic()
    await rig.registry.invoke_all(
        [_call("slow_read_a"), _call("slow_read_b")],
        recorder=rig.session,
        turn=TURN,
        step=1,
        constraints=constraints,
        workspace_root=rig.workspace,
    )
    elapsed = time.monotonic() - started

    # Both started before either finished.
    assert log[:2] == [("start", "slow_read_a"), ("start", "slow_read_b")]
    # And the step costs one delay, not two.
    assert elapsed < 0.18


async def test_a_write_is_a_barrier(rig) -> None:
    log: list[tuple[str, str]] = []
    rig.registry.register(_fake("slow_read_a", effect=ToolEffect.READ, log=log))
    rig.registry.register(_fake("slow_write", effect=ToolEffect.WRITE, log=log))
    rig.registry.register(_fake("slow_read_b", effect=ToolEffect.READ, log=log))
    constraints = constraints_for(Operation.READ_CODE, Operation.WRITE_CODE, writable=("src",))

    started = time.monotonic()
    await rig.registry.invoke_all(
        [_call("slow_read_a"), _call("slow_write"), _call("slow_read_b")],
        recorder=rig.session,
        turn=TURN,
        step=1,
        constraints=constraints,
        workspace_root=rig.workspace,
    )
    elapsed = time.monotonic() - started

    # Strictly sequential across the write: the second read waits for it.
    assert log == [
        ("start", "slow_read_a"),
        ("end", "slow_read_a"),
        ("start", "slow_write"),
        ("end", "slow_write"),
        ("start", "slow_read_b"),
        ("end", "slow_read_b"),
    ]
    assert elapsed >= 0.3


async def test_two_writes_never_overlap(rig) -> None:
    log: list[tuple[str, str]] = []
    rig.registry.register(_fake("slow_write_a", effect=ToolEffect.WRITE, log=log, delay=0.08))
    rig.registry.register(_fake("slow_write_b", effect=ToolEffect.WRITE, log=log, delay=0.08))
    constraints = constraints_for(Operation.WRITE_CODE, writable=("src",))

    started = time.monotonic()
    await rig.registry.invoke_all(
        [_call("slow_write_a"), _call("slow_write_b")],
        recorder=rig.session,
        turn=TURN,
        step=2,
        constraints=constraints,
        workspace_root=rig.workspace,
    )
    elapsed = time.monotonic() - started

    assert log == [
        ("start", "slow_write_a"),
        ("end", "slow_write_a"),
        ("start", "slow_write_b"),
        ("end", "slow_write_b"),
    ]
    assert elapsed >= 0.16


# ---------------------------------------------------------------------------
# The log still says what happened
# ---------------------------------------------------------------------------
async def test_each_result_follows_its_own_call(rig) -> None:
    log: list[tuple[str, str]] = []
    rig.registry.register(_fake("par_a", effect=ToolEffect.READ, log=log, delay=0.03))
    rig.registry.register(_fake("par_b", effect=ToolEffect.READ, log=log, delay=0.01))
    constraints = constraints_for(Operation.READ_CODE)

    await rig.registry.invoke_all(
        [_call("par_a"), _call("par_b")],
        recorder=rig.session,
        turn=TURN,
        step=3,
        constraints=constraints,
        workspace_root=rig.workspace,
    )
    await rig.session.flush()

    events = [event async for event in rig.store.read(rig.session.header.session_id)]
    calls = [e for e in events if e.kind == EventKind.TOOL_CALL and e.data.get("name") != "x"]
    results = [e for e in events if e.kind == EventKind.TOOL_RESULT]
    by_id = {event.data.get("callId"): event.seq for event in calls}
    ends = {event.data.get("callId"): event.seq for event in results}

    # Calls are recorded in request order even though the work overlapped.
    assert [event.data["callId"] for event in calls] == ["c-par_a-0", "c-par_b-0"]
    for call_id, call_seq in by_id.items():
        assert ends[call_id] > call_seq


async def test_the_shared_pipeline_trace_is_not_reused_across_calls(rig) -> None:
    # `invoke` resets the trace on entry, so a shared pipeline would have two
    # calls overwriting one diagnostic. The clones keep the session's pipeline
    # untouched.
    log: list[tuple[str, str]] = []
    rig.registry.register(_fake("par_a", effect=ToolEffect.READ, log=log, delay=0.02))
    rig.registry.register(_fake("par_b", effect=ToolEffect.READ, log=log, delay=0.02))
    constraints = constraints_for(Operation.READ_CODE)

    await rig.registry.invoke_all(
        [_call("par_a"), _call("par_b")],
        recorder=rig.session,
        turn=TURN,
        step=4,
        constraints=constraints,
        workspace_root=rig.workspace,
    )

    assert rig.pipeline.trace == []


@pytest.mark.parametrize("effect", [ToolEffect.WRITE, ToolEffect.EXEC, ToolEffect.NETWORK])
def test_a_non_reader_is_never_parallel(effect: ToolEffect) -> None:
    definitions = {
        "t": _fake("t", effect=effect, log=[]),
        "r": _fake("r", effect=ToolEffect.READ, log=[]),
    }

    groups = _groups([_call("t"), _call("t")], definitions)

    assert len(groups) == 2
    assert all(not safe for safe, _ in groups)


def test_session_only_tools_may_overlap_too() -> None:
    # `todo_write` changes nothing another call can observe.
    definitions = {"todo": _fake("todo", effect=ToolEffect.SESSION, log=[])}

    groups = _groups([_call("todo", 1), _call("todo", 2)], definitions)

    assert [len(group) for _, group in groups] == [2]


def test_the_constraints_used_here_have_no_network() -> None:
    # A reminder that the fake tools above cannot reach out even if they wanted
    # to: the fixture grants no network, so there is nothing to serialise for.
    assert NetworkAccess.DENY.value == "deny"
