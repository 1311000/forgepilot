"""What the estimate is calibrated on, and what that costs.

``estimate_tokens`` decides two things in production: whether the loop refuses a
request as too big for the window, and whether the compactor considers itself
finished. Both read its number as a fact, so a calibration that is wrong in the
low direction is the dangerous kind — the refusal and the compaction simply do
not fire, and the first symptom is a provider-side 400 at the far end of a turn.

For English the estimate has always been conservative (``len // 4`` over-counts
most English text). For CJK it was not: a CJK character is typically a whole
token, and this repository's tickets, briefs and ticket comments are routinely
CJK, so counting ``"中文" * 500`` as 250 tokens under-counted it by about 4×.

These tests pin both halves of the repair — the English arithmetic is untouched,
floor included, and a CJK character is no longer free.
"""

from __future__ import annotations

from forgepilot.runtime.context_size import estimate_tokens, measure
from forgepilot.seams.llm import ChatMessage, Role, ToolCallRequest


def test_ascii_is_still_four_characters_to_a_token() -> None:
    """The English calibration, unchanged — this is the number that must not move."""
    assert estimate_tokens((), ("a" * 400,)) == 100


def test_the_floor_still_happens_once_over_the_whole_request() -> None:
    """Four 5-character sections are 5 tokens, not 4.

    The repair has to keep one running total and one division at the end; a
    per-piece ``len // 4`` would round each section down and quietly change every
    English estimate that is not a multiple of four.
    """
    assert estimate_tokens((), ("a" * 5, "b" * 5, "c" * 5, "d" * 5)) == 5


def test_tool_call_arguments_are_counted_too() -> None:
    """Arguments are usually the largest part of an assistant message."""
    message = ChatMessage(
        role=Role.ASSISTANT,
        content="x" * 8,
        tool_calls=(ToolCallRequest(call_id="c1", name="abcd", arguments_json="y" * 8),),
    )
    assert estimate_tokens((message,), ()) == 5  # 8 + 8 + 4 characters


def test_a_cjk_character_costs_a_token() -> None:
    """The falsifier for the old calibration: this used to answer 250.

    ``"中文" * 500`` is 1000 characters, none of them ASCII. Under ``len // 4``
    the estimate was 250 tokens against a real cost near 1000 — a prompt four
    times the window could pass the check.
    """
    assert estimate_tokens((ChatMessage(role=Role.USER, content="中文" * 500),), ()) == 1000


def test_the_same_character_count_costs_more_in_cjk_than_in_english() -> None:
    """The property, rather than one magic number: width is per character."""
    cjk = ChatMessage(role=Role.USER, content="中" * 400)
    english = ChatMessage(role=Role.USER, content="a" * 400)
    assert estimate_tokens((cjk,), ()) == 400
    assert estimate_tokens((english,), ()) == 100


def test_mixed_content_counts_both_halves() -> None:
    """ASCII keeps its quarter, non-ASCII its whole, and the floor is shared."""
    system = ("a" * 400,)
    message = ChatMessage(role=Role.USER, content="中文" * 500)
    assert estimate_tokens((message,), system) == 1100  # 1000 + (400 // 4)


def test_non_ascii_tool_arguments_are_not_counted_as_english() -> None:
    """A CJK argument is the easiest place for the old under-count to hide."""
    message = ChatMessage(
        role=Role.ASSISTANT,
        tool_calls=(ToolCallRequest(call_id="c1", name="abcd", arguments_json="参数" * 50),),
    )
    assert estimate_tokens((message,), ()) == 101  # 100 wide + (4 // 4)


def test_measure_folds_the_log_through_the_same_estimate() -> None:
    """The compactor asks by event, the loop asks by message; one number rules."""
    assert measure([], ("a" * 400,)) == 100
