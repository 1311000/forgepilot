"""What a session spent, folded from its own log.

The loop appends one `turn/usage` event per model call (`EventKind.TURN_USAGE`),
so the answer to "what did this session cost" is a fold and not a counter. That
matters for the same reason the budget guard reads a tracker and the report reads
rows: the number a person is shown should be reconstructible from what was
recorded, not from whether the process that spent it is still running.

Deliberately not here: prices, currencies or per-model breakdowns. A provider
attaches `cost_usd` (it is the only thing that knows which model answered), so
this module adds up what the provider said and nothing else.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from forgepilot.seams.session import EventKind

__all__ = ["UsageTotals", "totals"]


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Summed over every `turn/usage` event in one session."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_payload(self) -> dict[str, Any]:
        """camelCase, like every other payload the API returns."""
        return {
            "calls": self.calls,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadTokens": self.cache_read_tokens,
            "tokens": self.tokens,
            "costUsd": self.cost_usd,
        }


def totals(events: Iterable[Any]) -> UsageTotals:
    """Fold `turn/usage` events. Anything else in the stream is ignored.

    A malformed payload is skipped rather than raised on: a log written by an
    older build, or a hand-edited one, must not make the endpoint that reports
    cost fail — the honest answer is the sum of what could be read.
    """
    calls = input_tokens = output_tokens = cache_read = 0
    cost = 0.0
    for event in events:
        if getattr(event, "kind", None) != EventKind.TURN_USAGE:
            continue
        data = getattr(event, "data", None)
        if not isinstance(data, Mapping):
            continue
        calls += 1
        input_tokens += _int(data.get("inputTokens"))
        output_tokens += _int(data.get("outputTokens"))
        cache_read += _int(data.get("cacheReadTokens"))
        cost += _float(data.get("costUsd"))
    return UsageTotals(
        calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cost_usd=cost,
    )


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)
