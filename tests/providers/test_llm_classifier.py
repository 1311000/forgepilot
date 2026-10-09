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
"""

from __future__ import annotations

import json

import pytest

from forgepilot.seams.classifier import ClassificationRequest
from forgepilot.seams.refs import TicketId

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
        ("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-1"}),
        ("forgepilot.providers.classify.llm:LLMClassifierPlugin", {}),
    ]:
        fibers.append(await root.plugin(resolve_plugin(spec), cfg))
    try:
        yield root.classifier, root.llm
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
    plugin, llm = classifier
    llm.script(answer())

    understanding = await plugin.classify(TICKET)

    assert understanding.category == "bug"
    assert understanding.confidence == pytest.approx(0.9)
    assert not understanding.needs_human, "a confident answer still asked for a human"
    assert "too few" in understanding.symptom


async def test_the_ticket_never_reaches_the_system_prompt(classifier) -> None:
    """The classifier copies ticket text into its output. If the ticket were in
    the system prompt, injected text would arrive already privileged."""
    plugin, llm = classifier
    llm.script(answer())

    await plugin.classify(TICKET)

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
    plugin, llm = classifier
    llm.script("I'm afraid I can't help with that.")

    understanding = await plugin.classify(TICKET)

    assert understanding.needs_human
    assert understanding.category == "", "a category was invented from nothing"
    assert understanding.confidence == 0.0


async def test_an_answer_missing_the_category_declines(classifier) -> None:
    plugin, llm = classifier
    llm.script(answer(category=""))

    assert (await plugin.classify(TICKET)).needs_human


async def test_a_low_confidence_answer_declines(classifier) -> None:
    plugin, llm = classifier
    llm.script(answer(confidence=0.1))

    assert (await plugin.classify(TICKET)).needs_human


async def test_a_model_that_reports_something_missing_declines(classifier) -> None:
    """`missing` is the model saying what it could not find. Proceeding anyway
    would be filling the gap with an assumption."""
    plugin, llm = classifier
    llm.script(answer(missing=["the reproduction steps"]))

    assert (await plugin.classify(TICKET)).needs_human


async def test_a_confidence_outside_zero_to_one_is_clamped_not_trusted(classifier) -> None:
    """A model returning 95 meaning 95% must not read as certainty beyond the
    scale the seam uses."""
    plugin, llm = classifier
    llm.script(answer(confidence=95))

    understanding = await plugin.classify(TICKET)

    assert 0.0 <= understanding.confidence <= 1.0


async def test_the_model_breaking_entirely_declines(classifier) -> None:
    """An endpoint that fails must send the ticket to a human, not take the task
    down with it."""
    plugin, llm = classifier
    llm.script()  # nothing scripted: the replay provider raises on use

    understanding = await plugin.classify(TICKET)

    assert understanding.needs_human
