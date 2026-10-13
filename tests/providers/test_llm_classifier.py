"""A classifier backed by the configured model.

The stub declines everything, which is the right test double but makes the whole
task flow unreachable: `plan_task` sends anything with `needs_human` to
`AWAITING_INFO`, and a declining classifier sets it on every ticket. So without a
classifier that actually classifies, task mode cannot run outside its tests.

Two properties matter more than the parsing:

- **A malformed answer declines.** Every failure path — bad JSON, a missing
  field, an exception — must end in `needs_human`, never in a guess. The
  classifier's output routes tools; a model guessing confidently there is the
  thing the seam exists to bound.
- **Ticket text goes in the user turn, not the system prompt.** A classifier
  copies ticket text into its output, and if that text arrived already
  privileged the injection would be speaking with the harness's voice.

And one property arrived with M16: **the call is recorded, or it is not made.**
The ticket is not history the log holds, so the provider writes a
`classifier/request` event carrying the prompt before it calls, and refuses
outright when there is no session to write to. Both halves are asserted below.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from forgepilot.runtime.session import Session
from forgepilot.seams.classifier import ClassificationRequest
from forgepilot.seams.refs import SessionId, TicketId
from forgepilot.seams.session import EventKind, SessionHeader, SurfaceOp

TICKET = ClassificationRequest(
    ticket_id=TicketId("T-1"),
    title="moving_average drops the last window",
    description="复现步骤：\n1. window=2, values=[1,2,3,4]\n\n预期结果：3 个窗口\n实际结果：2 个",
    signals={"paths": ["stats.py"]},
)


@pytest.fixture
async def classifier():
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fibers = []
    for spec, cfg in [
        # A real session, not a stand-in: the provider appends to it and the guard
        # flushes it, so a double would have to reimplement both.
        ("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin", {}),
        ("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-1"}),
        ("forgepilot.providers.classify.llm:LLMClassifierPlugin", {}),
    ]:
        fibers.append(await root.plugin(resolve_plugin(spec), cfg))
    try:
        sid = SessionId("classifier-under-test")
        await root.sessionPersistence.create(
            SessionHeader(
                session_id=sid,
                created_at=datetime.now(UTC),
                tenant="acme",
                principal="alice",
            )
        )
        session = await Session.open(root.sessionPersistence, sid)
        yield root.classifier, root.llm, session
    finally:
        for fiber in reversed(fibers):
            await dispose_fiber(fiber)


def answer(**fields) -> str:
    base = {
        "category": "bug",
        "symptom": "moving_average returns one window too few",
        "trigger": "window=2 with four values",
        "actual": "2 windows",
        "expected": "3 windows",
        "missing": [],
        "confidence": 0.9,
    }
    base.update(fields)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# It classifies
# ---------------------------------------------------------------------------
async def test_a_well_formed_answer_becomes_an_understanding(classifier) -> None:
    plugin, llm, session = classifier
    llm.script(answer())

    understanding = await plugin.classify(TICKET, session=session)

    assert understanding.category == "bug"
    assert understanding.confidence == pytest.approx(0.9)
    assert not understanding.needs_human, "a confident answer still asked for a human"
    assert "too few" in understanding.symptom


async def test_the_ticket_never_reaches_the_system_prompt(classifier) -> None:
    """The classifier copies ticket text into its output. If the ticket were in
    the system prompt, injected text would arrive already privileged."""
    plugin, llm, session = classifier
    llm.script(answer())

    await plugin.classify(TICKET, session=session)

    request = llm.requests[-1]
    system = " ".join(request.system)
    assert "moving_average" not in system, "ticket text leaked into the system prompt"
    assert any("moving_average" in (m.content or "") for m in request.messages), (
        "the ticket is not in the conversation at all"
    )


# ---------------------------------------------------------------------------
# Every failure declines
# ---------------------------------------------------------------------------
async def test_a_non_json_answer_declines_rather_than_guessing(classifier) -> None:
    plugin, llm, session = classifier
    llm.script("I'm afraid I can't help with that.")

    understanding = await plugin.classify(TICKET, session=session)

    assert understanding.needs_human
    assert understanding.category == "", "a category was invented from nothing"
    assert understanding.confidence == 0.0


async def test_an_answer_missing_the_category_declines(classifier) -> None:
    plugin, llm, session = classifier
    llm.script(answer(category=""))

    assert (await plugin.classify(TICKET, session=session)).needs_human


async def test_a_low_confidence_answer_declines(classifier) -> None:
    plugin, llm, session = classifier
    llm.script(answer(confidence=0.1))

    assert (await plugin.classify(TICKET, session=session)).needs_human


async def test_a_model_that_reports_something_missing_declines(classifier) -> None:
    """`missing` is the model saying what it could not find. Proceeding anyway
    would be filling the gap with an assumption."""
    plugin, llm, session = classifier
    llm.script(answer(missing=["the reproduction steps"]))

    assert (await plugin.classify(TICKET, session=session)).needs_human


async def test_a_confidence_outside_zero_to_one_is_clamped_not_trusted(classifier) -> None:
    """A model returning 95 meaning 95% must not read as certainty beyond the
    scale the seam uses."""
    plugin, llm, session = classifier
    llm.script(answer(confidence=95))

    understanding = await plugin.classify(TICKET, session=session)

    assert 0.0 <= understanding.confidence <= 1.0


async def test_the_model_breaking_entirely_declines(classifier) -> None:
    """An endpoint that fails must send the ticket to a human, not take the task
    down with it."""
    plugin, llm, session = classifier
    llm.script()  # nothing scripted: the replay provider raises on use

    understanding = await plugin.classify(TICKET, session=session)

    assert understanding.needs_human


# ---------------------------------------------------------------------------
# The call is recorded, or it is not made (M16)
# ---------------------------------------------------------------------------
async def test_the_prompt_is_logged_before_the_model_is_asked(classifier) -> None:
    """The ticket is not in the log until this call puts it there.

    So the event is what makes the call reconstructable, and the test asserts the
    two halves that makes true: the log holds the prompt **verbatim**, and the
    message the model received cites that same event.
    """
    plugin, llm, session = classifier
    llm.script(answer())

    await plugin.classify(TICKET, session=session)

    logged = [e for e in session.committed_events if e.kind == EventKind.CLASSIFIER_REQUEST]
    assert len(logged) == 1, "the classification was not recorded exactly once"
    event = logged[0]
    assert "moving_average" in event.data["prompt"], "the ticket is not in the event"
    assert event.data["model"] == llm.requests[-1].model

    sent = llm.requests[-1]
    assert sent.messages[0].content == event.data["prompt"], (
        "the model was shown something other than what the log recorded"
    )
    assert sent.messages[0].source_event_seq == event.seq
    assert event.surface is SurfaceOp.LOG_ONLY, (
        "a later turn would see what this call asked, which is not history"
    )


async def test_without_a_session_the_model_is_not_asked(classifier) -> None:
    """Fail closed: an unrecorded model call is the thing the invariant forbids.

    Asserting `len(llm.requests)` rather than the understanding is the point — a
    provider that called the model and then declined would pass the weaker
    assertion while making exactly the call this refuses to make.
    """
    plugin, llm, _session = classifier
    llm.script(answer())

    understanding = await plugin.classify(TICKET)

    assert llm.requests == [], "a model call was made with nowhere to record it"
    assert understanding.needs_human
    assert "session" in understanding.notes
