"""Compaction as the loop actually calls it.

The tests above call `compact()` directly. These go through `run_step`, which is
where two things live that calling the compactor cannot show:

- **The pass must run before `build_request`, not after.** `build_request` writes a
  log-only `request/header` first, so compacting afterwards would leave a header
  describing a request that was never sent — a record that is simply false. And
  `guarded_stream` flushes then requires every message's `source_event_seq` to be
  durable, so a compaction event appended after the request was derived would
  either be invisible to it or trip `ModelVisibleNotLogged`.
- **`compactor=None` must be exactly what the loop did before.** Every caller that
  has not opted in — and every existing test — depends on that.

The window is replaced with a small one deliberately: the replay model reports
200_000 tokens, and inflating a fixture past that to reach the threshold would
test the fixture rather than the loop.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from forgepilot.runtime.loop.step import ContextWindowExceeded, run_step
from forgepilot.seams.llm import Role
from forgepilot.seams.refs import PrincipalId, TenantId, TurnId
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal
from forgepilot.seams.session import EventKind
from tests.compaction.test_tiered_compaction import SECTIONS, build, close, make_session

WINDOW = 800


class SmallWindow:
    """The scripted model, reporting a window small enough to reach.

    A wrapper rather than a config change: `ReplayLLMPlugin` hardcodes its
    capability, and giving it a knob would be production surface existing only for
    a test. `capabilities` is the one method overridden; everything else — `stream`,
    `complete`, `requests`, `script` — passes through untouched, so the model
    behaves exactly as it does everywhere else.
    """

    def __init__(self, inner: Any, window: int) -> None:
        self._inner = inner
        self._window = window

    def capabilities(self) -> Any:
        return replace(self._inner.capabilities(), max_context_tokens=self._window)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def constraints() -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        permitted_operations=frozenset(Operation),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


async def step(root: Any, session: Any, *, compactor: Any, llm: Any = None) -> Any:
    model = llm if llm is not None else SmallWindow(root.llm, WINDOW)
    root.llm.script("done")
    return await run_step(
        session=session,
        llm=model,
        tools=None,  # a text-only turn requests no tools
        constraints=constraints(),
        turn=TurnId(99),
        step=0,
        model="replay-1",
        system_sections=SECTIONS,
        compactor=compactor,
    )


async def test_a_conversation_that_would_not_fit_survives_with_compaction() -> None:
    """The entire point, stated as a before-and-after on the same conversation."""
    root, fibers = await build(
        {"tiers": ["mask"], "keep_recent_tool_results": 1, "trigger_fraction": 0.1}
    )
    try:
        session = await make_session(root, turns=8)
        before = len(session.committed_events)

        result = await step(root, session, compactor=root.compaction)

        assert result.text == "done"
        assert len(session.committed_events) > before, "nothing was compacted"
        kinds = [e.kind for e in session.committed_events]
        assert EventKind.COMPACTION_MASK in kinds
    finally:
        await close(fibers)


async def test_without_a_compactor_the_old_loud_failure_is_unchanged() -> None:
    """Every caller that has not opted in gets exactly what it got before.

    `None` is the default argument, so this is also what every existing test and
    every profile with no `compaction` row exercises.
    """
    root, fibers = await build()
    try:
        session = await make_session(root, turns=8)

        with pytest.raises(ContextWindowExceeded) as caught:
            await step(root, session, compactor=None)

        assert "Compaction ran" not in str(caught.value), (
            "the message blames compaction for a run that had none"
        )
    finally:
        await close(fibers)


async def test_the_pass_is_written_before_the_request_header() -> None:
    """The ordering, asserted on the log rather than on the call order.

    A `request/header` written before the compaction would describe a request that
    was never sent — and the events the compaction appended would not be durable
    when `assert_logged` checked, so the step would die with
    `ModelVisibleNotLogged` instead of sending a smaller request.
    """
    root, fibers = await build(
        {"tiers": ["mask"], "keep_recent_tool_results": 1, "trigger_fraction": 0.1}
    )
    try:
        session = await make_session(root, turns=8)

        await step(root, session, compactor=root.compaction)

        events = session.committed_events
        starts = [e.seq for e in events if e.kind == EventKind.COMPACTION_START]
        headers = [e.seq for e in events if e.kind == EventKind.REQUEST_HEADER]
        assert starts, "the pass never ran"
        assert headers, "no request header was written"
        assert max(starts) < max(headers), (
            "the compaction was written after the request header, so the header "
            "describes a request that was never sent"
        )
        assert EventKind.COMPACTION_END in [e.kind for e in events]
    finally:
        await close(fibers)


async def test_the_request_actually_sent_is_smaller_and_still_valid() -> None:
    """Compaction has to change what reaches the model, not just the log."""
    root, fibers = await build(
        {"tiers": ["mask"], "keep_recent_tool_results": 1, "trigger_fraction": 0.1}
    )
    try:
        session = await make_session(root, turns=8)

        await step(root, session, compactor=root.compaction)

        sent = root.llm.requests[-1]
        body = "\n".join(m.content or "" for m in sent.messages)
        assert "[tool output elided" in body, "the model was sent the uncompacted history"

        # And the request is well formed: every tool call is answered immediately
        # after it. This is the assertion that catches a compaction splitting a
        # pair, which the endpoint would reject one turn later.
        for index, message in enumerate(sent.messages):
            if not message.tool_calls:
                continue
            wanted = [call.call_id for call in message.tool_calls]
            following = sent.messages[index + 1 : index + 1 + len(wanted)]
            answered = [m.tool_call_id for m in following if m.role is Role.TOOL]
            assert answered == wanted, (
                f"message {index} asked for {wanted} and is followed by {following}"
            )
    finally:
        await close(fibers)
