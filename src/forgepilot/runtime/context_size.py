"""How big is the conversation, roughly.

Two callers ask this and they must agree: the loop, deciding whether a request
will fit, and the compactor, deciding whether it freed enough to stop. Two
estimators would drift, and the symptom of drifting would be a compaction that
reports success and changes nothing — or one that runs on every single step.

It lived in ``loop/step.py`` until the compactor needed it too. A provider
importing the loop's internals would have been the alternative, and that gets the
dependency backwards: the loop drives the compactor, not the other way round.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from forgepilot.seams.llm import ChatMessage

__all__ = ["estimate_tokens", "measure"]


def _wider_than_ascii(text: str) -> int:
    """How many characters in ``text`` cost more than the English calibration.

    A count, not a weight: the caller keeps one running total of characters and
    one of these, so that the single floor at the end is the floor the ASCII-only
    estimator has always applied. Per-piece rounding would quietly change English
    numbers, and the whole point of this change is that it cannot.
    """
    return sum(1 for char in text if not char.isascii())


def estimate_tokens(messages: tuple[ChatMessage, ...], system: tuple[str, ...]) -> int:
    """A deliberately crude size estimate — roughly four ASCII characters per token.

    Crude on purpose: a real tokenizer is a dependency and a per-model decision,
    and this exists only to refuse a request that would obviously blow the window
    rather than to bill anyone.

    The calibration is English. ``len(text) // 4`` is a fair guess at English and
    a bad one at Chinese: a CJK character is typically a whole token, so counting
    ``"中文" * 500`` as 250 tokens against a real cost near 1000 refuses roughly
    three times too late — the unsafe direction, in a system whose tickets and
    briefs are routinely CJK. A non-ASCII character therefore counts as one token
    and ASCII keeps exactly the arithmetic it had, floor included, so no
    English-only measurement moves.

    Where that leaves the bias: for English it still errs toward refusing early,
    which is the safe direction; for CJK it now errs the same way. The compactor
    inherits that bias and it stays safe there for the same reason: compacting one
    step earlier than necessary costs a little, and compacting one step too late
    has already failed.
    """
    total = 0
    wide = 0
    for part in system:
        total += len(part)
        wide += _wider_than_ascii(part)
    for message in messages:
        content = message.content or ""
        total += len(content)
        wide += _wider_than_ascii(content)
        for call in message.tool_calls:
            total += len(call.arguments_json) + len(call.name)
            wide += _wider_than_ascii(call.arguments_json) + _wider_than_ascii(call.name)
    return wide + (total - wide) // 4


def measure(events: Sequence[Any], system_sections: Sequence[str]) -> int:
    """What the next request would cost, without building it.

    Folds the log the way the projection would and estimates the result. Imports
    the fold lazily because ``projection`` imports this module's sibling
    vocabulary rather than the reverse — the fold is the expensive half and it is
    not needed to know what a sequence of events *weighs*.
    """
    from forgepilot.runtime.projection import derive_messages

    return estimate_tokens(derive_messages(events), tuple(system_sections))
