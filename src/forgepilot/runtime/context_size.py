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


def estimate_tokens(messages: tuple[ChatMessage, ...], system: tuple[str, ...]) -> int:
    """A deliberately crude size estimate — roughly four characters per token.

    Crude on purpose: a real tokenizer is a dependency and a per-model decision,
    and this exists only to refuse a request that would obviously blow the window
    rather than to bill anyone. When it is wrong it is wrong in the direction of
    refusing early, which is the safe direction.

    The compactor inherits that bias and it stays safe there for the same reason:
    compacting one step earlier than necessary costs a little, and compacting one
    step too late has already failed.
    """
    total = sum(len(part) for part in system)
    for message in messages:
        total += len(message.content or "")
        for call in message.tool_calls:
            total += len(call.arguments_json) + len(call.name)
    return total // 4


def measure(events: Sequence[Any], system_sections: Sequence[str]) -> int:
    """What the next request would cost, without building it.

    Folds the log the way the projection would and estimates the result. Imports
    the fold lazily because ``projection`` imports this module's sibling
    vocabulary rather than the reverse — the fold is the expensive half and it is
    not needed to know what a sequence of events *weighs*.
    """
    from forgepilot.runtime.projection import derive_messages

    return estimate_tokens(derive_messages(events), tuple(system_sections))
