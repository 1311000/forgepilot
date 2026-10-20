"""What a session spent is a fold of its log, not a live counter (P8).

The endpoint that reports cost must answer for a session whose process is gone —
after a restart, from a client, over a history entry. That is only possible if the
number is reconstructible from what was recorded, so `totals()` is deliberately
paranoid about the payload instead of trusting it: a log written by an older
build, or one a person edited, must not make the endpoint fail.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from forgepilot.runtime.usage import totals
from forgepilot.seams.session import EventKind


def event(kind: str, data: Any) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, data=data)


def usage(**overrides: Any) -> SimpleNamespace:
    data = {
        "inputTokens": 10,
        "outputTokens": 5,
        "cacheReadTokens": 100,
        "costUsd": 0.25,
    }
    data.update(overrides)
    return event(EventKind.TURN_USAGE, data)


def test_a_session_that_never_called_a_model_spent_nothing() -> None:
    totals_ = totals([])
    assert totals_.calls == 0
    assert totals_.tokens == 0
    assert totals_.cost_usd == 0.0


def test_only_usage_events_are_counted() -> None:
    events = [
        event(EventKind.TURN_START, {"turn": 0}),
        usage(),
        event(EventKind.ASSISTANT_MESSAGE, {"text": "hello"}),
        usage(inputTokens=1, outputTokens=2, cacheReadTokens=0, costUsd=0.5),
    ]
    totals_ = totals(events)
    assert totals_.calls == 2
    assert (totals_.input_tokens, totals_.output_tokens) == (11, 7)
    assert totals_.cache_read_tokens == 100
    assert totals_.cost_usd == 0.75


def test_a_payload_that_is_not_a_mapping_is_skipped_rather_than_raised_on() -> None:
    events = [event(EventKind.TURN_USAGE, "this is not a payload"), usage()]
    assert totals(events).calls == 1


def test_a_missing_or_wrong_typed_field_counts_as_zero() -> None:
    events = [
        event(EventKind.TURN_USAGE, {"inputTokens": "ten", "outputTokens": None}),
        event(EventKind.TURN_USAGE, {"costUsd": "free"}),
    ]
    totals_ = totals(events)
    assert (totals_.input_tokens, totals_.output_tokens, totals_.cost_usd) == (0, 0, 0.0)


def test_a_boolean_is_not_a_number() -> None:
    """`True` is an `int` in Python; summing it into a token count would be a lie
    that type checks."""
    assert totals([event(EventKind.TURN_USAGE, {"inputTokens": True})]).input_tokens == 0


def test_the_payload_is_camel_case_like_the_rest_of_the_api() -> None:
    payload = totals([usage()]).to_payload()
    assert payload == {
        "calls": 1,
        "inputTokens": 10,
        "outputTokens": 5,
        "cacheReadTokens": 100,
        "tokens": 15,
        "costUsd": 0.25,
    }
