"""Compaction: what it replaces, what it refuses to, and what it leaves behind.

Two properties run through every test here, and they are the ones that make this
safe rather than merely useful:

- **Nothing is destroyed.** A pass appends events. The history it covers stays in
  the log byte for byte, and the model's view of it is *derived* — so a run that
  went wrong is still diagnosable afterwards, and an auditor can still grep the
  content the model was no longer shown.
- **No pair is ever split.** An assistant message whose ``tool_calls`` is not
  answered immediately after it is rejected outright by the endpoint, and the
  error arrives **one turn later** with a traceback pointing at the request
  rather than at the cut. Masking cannot cause it (it never touches an assistant
  message) and summarising is kept from it by working in whole turns.

The thresholds in these tests are tiny on purpose — `estimate_tokens` is
`len // 4`, so a few hundred characters is enough to cross a small window.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import pytest

from forgepilot.providers.llm.replay import text_turn
from forgepilot.runtime.context_size import measure
from forgepilot.runtime.projection import derive_messages
from forgepilot.runtime.session import Session
from forgepilot.seams.compaction import (
    FAILURE_CIRCUIT_OPEN,
    FAILURE_EMPTY,
    FAILURE_NO_REGION,
    FAILURE_TRUNCATED,
    CompactionTier,
)
from forgepilot.seams.llm import Role, Usage
from forgepilot.seams.refs import SessionId, TurnId
from forgepilot.seams.session import EventKind, SessionHeader, SessionEvent, SurfaceOp

BIG = "line of output\n" * 120  # ~1800 chars, ~450 estimated tokens


async def build(config: dict | None = None):
    """A root with a compaction provider, a session store and a scripted model."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root

    root = create_root()
    fibers = []

    async def load(spec: str, cfg: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), cfg or {})
        fibers.append(fiber)
        return fiber

    await load("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin")
    await load("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-1"})
    await load("forgepilot.providers.compaction.tiered:TieredCompactionPlugin", config or {})
    return root, fibers


async def close(fibers) -> None:
    from forgepilot.kernel import dispose_fiber

    for fiber in reversed(fibers):
        await dispose_fiber(fiber)


async def make_session(root: Any, *, turns: int = 4, brief: str = "fix the bug") -> Session:
    """A brief, then ``turns`` call/result pairs whose results are all large."""
    sid = SessionId("compact-under-test")
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=sid, created_at=datetime.now(timezone.utc), tenant="acme", principal="alice"
        )
    )
    session = await Session.open(root.sessionPersistence, sid)
    await session.turn_start(TurnId(1))
    await session.user_message(TurnId(1), brief)
    await session.turn_end(TurnId(1), "completed")
    for turn in range(2, turns + 2):
        call = f"c{turn}"
        await session.turn_start(TurnId(turn))
        await session.assistant_message(
            TurnId(turn), 1, "", tool_calls=[{"callId": call, "name": "read_file", "arguments": "{}"}]
        )
        await session.tool_call(TurnId(turn), 1, call_id=call, name="read_file", arguments_json="{}")
        await session.tool_result(TurnId(turn), 1, call_id=call, content=f"src/f{turn}.py\n{BIG}")
        await session.turn_end(TurnId(turn), "completed")
    await session.flush()
    return session


SECTIONS = ("you are forgepilot",)


def assert_no_orphan_tool_calls(messages: Sequence[Any]) -> None:
    for index, message in enumerate(messages):
        if message.role is not Role.ASSISTANT or not message.tool_calls:
            continue
        wanted = [call.call_id for call in message.tool_calls]
        following = messages[index + 1 : index + 1 + len(wanted)]
        answered = [m.tool_call_id for m in following if m.role is Role.TOOL]
        assert answered == wanted, (
            f"assistant message {index} asked for {wanted}, followed by "
            f"{[str(m.role) for m in following]} answering {answered}"
        )


async def compact(root: Any, session: Session, *, window: int):
    """Run one pass against a session, the way `run_step` does."""
    return await root.compaction.compact(
        session=session,
        llm=root.llm,
        model="replay-1",
        system_sections=SECTIONS,
        turn=TurnId(1),
        step=0,
        estimated_tokens=measure(session.events, SECTIONS),
        window_tokens=window,
    )


def result_seqs(session: Session, surface: SurfaceOp) -> list[int]:
    return [
        e.seq
        for e in session.committed_events
        if e.kind == EventKind.TOOL_RESULT and e.surface is surface
    ]


# ---------------------------------------------------------------------------
# The trigger
# ---------------------------------------------------------------------------
async def test_under_the_threshold_nothing_is_written() -> None:
    """A pass that is not needed must be invisible — no events, no cost."""
    root, fibers = await build()
    try:
        session = await make_session(root)
        before = len(session.committed_events)

        outcome = await compact(root, session, window=10_000_000)

        assert not outcome.performed
        assert outcome.tier is CompactionTier.NONE
        assert len(session.committed_events) == before, "a no-op pass wrote to the log"
    finally:
        await close(fibers)


async def test_the_reserve_fires_before_the_fraction_on_a_small_window() -> None:
    """`reserve_tokens` is the condition that matters when the window is small.

    A percentage alone leaves no room for the reply that is about to be asked for:
    at 99% of an 8k window there is not enough left to answer in.
    """
    # Enough turns that masking has something to do: the default keeps the newest
    # four results, so a four-turn session has nothing eligible at all.
    root, fibers = await build(
        {"trigger_fraction": 0.99, "reserve_tokens": 100_000, "tiers": ["mask"]}
    )
    try:
        session = await make_session(root, turns=8)
        before = len(session.committed_events)

        outcome = await compact(root, session, window=1_000)

        assert outcome.performed, f"the reserve condition did not fire: {outcome.detail}"
        assert len(session.committed_events) > before
    finally:
        await close(fibers)


# ---------------------------------------------------------------------------
# Tier 1 — masking
# ---------------------------------------------------------------------------
async def test_masking_replaces_old_bodies_and_reduces_the_measurement() -> None:
    """The claim is "it freed context", so it is measured, not assumed."""
    root, fibers = await build({"tiers": ["mask"], "keep_recent_tool_results": 1})
    try:
        session = await make_session(root, turns=5)
        before = measure(session.events, SECTIONS)

        outcome = await compact(root, session, window=200)

        assert outcome.performed
        assert outcome.tier is CompactionTier.MASK
        after = measure(session.events, SECTIONS)
        assert after < before, f"nothing was freed: {before} -> {after}"
        assert outcome.estimated_after == after

        kinds = [e.kind for e in session.committed_events]
        assert EventKind.COMPACTION_START in kinds
        assert EventKind.COMPACTION_MASK in kinds
        assert EventKind.COMPACTION_END in kinds
    finally:
        await close(fibers)


async def test_masking_never_breaks_a_call_from_its_result() -> None:
    """The failure this tier is shaped to make impossible.

    Masking only ever replaces a ``tool/result``, carrying the *same call id*, so
    the answer stays where the answer was and an assistant message is never
    touched at all.
    """
    root, fibers = await build({"tiers": ["mask"], "keep_recent_tool_results": 0})
    try:
        session = await make_session(root, turns=4)

        outcome = await compact(root, session, window=200)

        assert outcome.shadowed, "nothing was masked, so this would prove nothing"
        assert_no_orphan_tool_calls(derive_messages(session.events))
        placeholders = result_seqs(session, SurfaceOp.REPLACE)
        assert placeholders, "no placeholder reached the log"
        assert all(
            e.data.get("masked")
            for e in session.committed_events
            if e.seq in placeholders
        )
    finally:
        await close(fibers)


async def test_the_original_bodies_are_still_in_the_log() -> None:
    """Compaction-instead-of-mutation, and it is the reason to prefer it.

    The model stops seeing the body. The log keeps every byte, so the run stays
    diagnosable and `read_file` can fetch the file again.
    """
    root, fibers = await build({"tiers": ["mask"], "keep_recent_tool_results": 0})
    try:
        session = await make_session(root, turns=3)

        await compact(root, session, window=200)

        originals = [
            str(e.data.get("content", ""))
            for e in session.committed_events
            if e.kind == EventKind.TOOL_RESULT and e.surface is SurfaceOp.APPEND
        ]
        assert originals, "the original results were removed rather than shadowed"
        assert any(BIG.strip() in body for body in originals)
    finally:
        await close(fibers)


async def test_the_task_brief_is_never_masked() -> None:
    """Without it the run has no statement of what it is for."""
    root, fibers = await build({"tiers": ["mask"], "keep_recent_tool_results": 0})
    try:
        session = await make_session(root, turns=3, brief="fix the login loop")

        await compact(root, session, window=200)

        contents = [m.content or "" for m in derive_messages(session.events)]
        assert "fix the login loop" in contents
    finally:
        await close(fibers)


async def test_the_newest_tool_results_are_left_alone() -> None:
    """They are what the model is reasoning about right now."""
    root, fibers = await build({"tiers": ["mask"], "keep_recent_tool_results": 2})
    try:
        session = await make_session(root, turns=5)
        oldest = result_seqs(session, SurfaceOp.APPEND)

        outcome = await compact(root, session, window=200)

        covered = {
            e.data["supersedes"][0]
            for e in session.committed_events
            if e.kind == EventKind.TOOL_RESULT and e.surface is SurfaceOp.REPLACE
        }
        assert outcome.performed
        assert covered == set(oldest[:-2]), (
            f"masked {sorted(covered)} of {oldest}; the newest two must be left alone"
        )
    finally:
        await close(fibers)


# ---------------------------------------------------------------------------
# Tier 2 — summarising
# ---------------------------------------------------------------------------
async def test_a_summary_replaces_whole_turns_and_lands_in_their_place() -> None:
    root, fibers = await build(
        {"tiers": ["summary"], "keep_recent_turns": 1, "summary_min_region_tokens": 1}
    )
    try:
        session = await make_session(root, turns=4)
        root.llm.script("<analysis>scratch</analysis><summary>\n## Objective\nfix it\n</summary>")

        outcome = await compact(root, session, window=200)

        assert outcome.performed, outcome.detail
        assert outcome.tier is CompactionTier.SUMMARY
        messages = derive_messages(session.events)
        assert messages[0].content == "fix the bug", "the brief moved or was covered"
        bodies = [m.content or "" for m in messages]
        assert any("[compacted conversation" in body for body in bodies)
        assert "scratch" not in "\n".join(bodies), "the analysis scratchpad was kept"
        assert_no_orphan_tool_calls(messages)
    finally:
        await close(fibers)


async def test_a_truncated_summary_is_discarded_rather_than_kept() -> None:
    """**Fail closed.** DSH #3201 is the case: a checkpoint cut off at the token
    cap was kept, and it gutted the session on resume.

    The check is on the usage we asked for rather than on `stop_reason` — no
    provider populates `stop_reason`, so a check on it would never fire.
    """
    root, fibers = await build(
        {
            "tiers": ["summary"],
            "keep_recent_turns": 1,
            "summary_max_tokens": 64,
            "summary_min_region_tokens": 1,
        }
    )
    try:
        session = await make_session(root, turns=4)
        root.llm.script(text_turn("<summary>half a handoff", usage=Usage(output_tokens=64)))
        before = derive_messages(session.events)

        outcome = await compact(root, session, window=200)

        assert not outcome.performed
        assert outcome.failure == FAILURE_TRUNCATED, outcome.failure
        assert derive_messages(session.events) == before, (
            "a truncated checkpoint reached the conversation"
        )
        kinds = [e.kind for e in session.committed_events]
        assert EventKind.COMPACTION_FAILED in kinds, "the failure left no trace"
    finally:
        await close(fibers)


async def test_an_empty_summary_is_discarded() -> None:
    root, fibers = await build(
        {"tiers": ["summary"], "keep_recent_turns": 1, "summary_min_region_tokens": 1}
    )
    try:
        session = await make_session(root, turns=4)
        root.llm.script("<summary>   </summary>")
        before = derive_messages(session.events)

        outcome = await compact(root, session, window=200)

        assert not outcome.performed
        assert outcome.failure == FAILURE_EMPTY
        assert derive_messages(session.events) == before
    finally:
        await close(fibers)


async def test_a_region_below_the_floor_is_not_summarised() -> None:
    """A summary that costs what it saves is a loss with a model call attached."""
    root, fibers = await build({"tiers": ["summary"], "summary_min_region_tokens": 10_000_000})
    try:
        session = await make_session(root, turns=4)

        outcome = await compact(root, session, window=200)

        assert not outcome.performed
        assert outcome.failure == FAILURE_NO_REGION
        assert root.llm.requests == [], "the model was called for a pass that was refused"
    finally:
        await close(fibers)


async def test_a_summary_is_never_summarised_again() -> None:
    """Errors compound across passes, and no budget makes that acceptable.

    The barrier is the last **summary**, not the last REPLACE: a mask placeholder
    is not a summary, and refusing to summarise a region because it contains one
    would stop the two tiers composing at all.
    """
    root, fibers = await build(
        {"tiers": ["summary"], "keep_recent_turns": 1, "summary_min_region_tokens": 1}
    )
    try:
        session = await make_session(root, turns=4)
        root.llm.script("<summary>first handoff</summary>")
        first = await compact(root, session, window=200)
        assert first.performed, first.detail

        summaries = [
            e for e in session.committed_events if e.kind == EventKind.COMPACTION_SUMMARY
        ]
        assert len(summaries) == 1
        barrier = summaries[0].seq

        # Add fresh history *after* the summary, and make the window tight again.
        await session.turn_start(TurnId(9))
        await session.user_message(TurnId(9), "x" * 4_000)
        await session.turn_end(TurnId(9), "completed")
        await session.turn_start(TurnId(10))
        await session.user_message(TurnId(10), "y" * 4_000)
        await session.turn_end(TurnId(10), "completed")
        await session.flush()

        root.llm.script("<summary>second handoff</summary>")
        second = await compact(root, session, window=300)

        if second.performed:
            low, _ = second.superseded
            assert low > barrier, (
                f"the second pass covered the first summary (region starts at {low}, "
                f"the summary is at {barrier}) — a summary of a summary"
            )
    finally:
        await close(fibers)


# ---------------------------------------------------------------------------
# The breaker
# ---------------------------------------------------------------------------
async def test_the_breaker_opens_after_the_limit_and_stops_calling_the_model() -> None:
    """Without it, a session that cannot get under the window compacts, fails to,
    and compacts again — and thrashing costs more than the failure it avoids."""
    root, fibers = await build(
        {
            "tiers": ["summary"],
            "keep_recent_turns": 1,
            "failure_limit": 2,
            "summary_min_region_tokens": 1,
        }
    )
    try:
        session = await make_session(root, turns=4)

        for expected in (FAILURE_EMPTY, FAILURE_EMPTY):
            root.llm.script("<summary>   </summary>")
            outcome = await compact(root, session, window=200)
            assert outcome.failure == expected, outcome.failure

        calls_before = len(root.llm.requests)
        third = await compact(root, session, window=200)

        assert third.failure == FAILURE_CIRCUIT_OPEN
        assert len(root.llm.requests) == calls_before, (
            "the breaker let another summariser call through"
        )
    finally:
        await close(fibers)


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------
def test_mask_placeholder_keeps_both_ends_and_the_size() -> None:
    from forgepilot.providers.compaction.tiered import mask_placeholder

    body = "HEAD" + ("." * 500) + "TAIL"
    placeholder = mask_placeholder("read_file", body)

    assert "read_file" in placeholder
    assert str(len(body)) in placeholder
    assert "HEAD" in placeholder and "TAIL" in placeholder
    assert len(placeholder) < len(body)
    # A body with nothing worth eliding comes back untouched, so a pass over it
    # saves nothing and is skipped rather than logged as work.
    assert mask_placeholder("run", "short") == "short"


def test_snap_to_pairs_widens_rather_than_cutting() -> None:
    from forgepilot.providers.compaction.tiered import snap_to_pairs

    def ev(seq, kind, cid):
        return SessionEvent(
            seq=seq, event_id=f"e{seq}", kind=kind, data={"callId": cid}, surface=SurfaceOp.APPEND
        )

    events = [
        ev(1, EventKind.TOOL_CALL, "a"),
        ev(2, EventKind.TOOL_RESULT, "a"),
        ev(3, EventKind.TOOL_CALL, "b"),
        ev(4, EventKind.TOOL_RESULT, "b"),
    ]

    # Keeping b's call while dropping its result is widened to include both — an
    # unanswered call is rejected outright by the endpoint.
    assert snap_to_pairs(events, 3, 3) == (3, 4)
    # And dropping a call while keeping its result widens the other way.
    assert snap_to_pairs(events, 2, 4) == (1, 4)
    # Already whole: untouched.
    assert snap_to_pairs(events, 1, 2) == (1, 2)


def test_consecutive_failures_counts_from_the_log() -> None:
    """Read from the log, not memory — a resumed session is exactly the one most
    likely to be near its window, and an in-memory counter would have reset."""
    from forgepilot.providers.compaction.tiered import consecutive_failures

    def ev(seq, kind):
        return SessionEvent(seq=seq, event_id=f"e{seq}", kind=kind, data={}, surface=SurfaceOp.LOG_ONLY)

    assert consecutive_failures([]) == 0
    assert consecutive_failures([ev(1, EventKind.COMPACTION_FAILED)]) == 1
    assert (
        consecutive_failures(
            [
                ev(1, EventKind.COMPACTION_FAILED),
                ev(2, EventKind.COMPACTION_FAILED),
                ev(3, EventKind.COMPACTION_SUMMARY),  # a pass that worked
                ev(4, EventKind.COMPACTION_FAILED),
            ]
        )
        == 1
    )
    assert (
        consecutive_failures(
            [
                ev(1, EventKind.COMPACTION_MASK),
                ev(2, EventKind.COMPACTION_FAILED),
                ev(3, EventKind.COMPACTION_FAILED),
            ]
        )
        == 2
    )
