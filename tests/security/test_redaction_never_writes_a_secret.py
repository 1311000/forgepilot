"""The gate as the *log* sees it: nothing sensitive is ever written.

The claim this file defends is the one D2 was decided on. Because whatever the
model sees must already be in the log (``runtime/log_guard.py``), a secret that
never entered the log can never reach the model — one gate at ``Session.append``
covers the conversation and the storage together. The rule set itself is tested in
``test_redaction_rules.py``; what is asserted here is the wiring, and that a
detector failing degrades the *data* rather than the *run*.

The blob test is not decoration. A payload above the store's inline limit spills
to a file, and a gate placed at the storage layer would miss every one of them —
which is exactly why the gate sits in ``append()``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forgepilot.config import resolve_plugin
from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.runtime.redaction import (
    FAILED_PREFIX,
    PLACEHOLDER_PREFIX,
    Redactor,
    Rule,
)
from forgepilot.runtime.session import Session
from forgepilot.seams.refs import SessionId, TurnId
from forgepilot.seams.security import Sensitivity
from forgepilot.seams.session import EventKind, SessionEvent, SessionHeader

#: A value shaped like a GitHub token. Not a real credential — and it must never
#: be written, which is the whole point.
SECRET = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

#: An intranet host, i.e. an ``INTERNAL``-classified match. The blob test uses this
#: rather than a token on purpose: a *SECRET*-classified match collapses the whole
#: payload to a stub, so a token here would never reach a blob file and the test
#: would quietly stop covering the spill path it exists for.
INTERNAL_HOST = "git.acme.internal"

#: The store's inline limit, restated here so the blob test can prove the payload
#: really spilled rather than quietly staying inline.
INLINE_LIMIT = 64 * 1024


def _dumped(events: list[SessionEvent]) -> str:
    return json.dumps([event.to_json() for event in events], ensure_ascii=False, default=str)


async def _events(store: Any, sid: SessionId) -> list[SessionEvent]:
    return [event async for event in store.read(sid)]


@pytest.fixture(params=["memory", "jsonl"])
async def created(request: pytest.FixtureRequest, tmp_path: Path):
    """A store, a session that exists in it, and the directory it writes under."""
    spec = {
        "memory": "forgepilot.providers.session_store.memory:InMemorySessionStorePlugin",
        "jsonl": "forgepilot.providers.session_store.jsonl:JsonlSessionStorePlugin",
    }[request.param]
    config = {"root": str(tmp_path / "sessions")} if request.param == "jsonl" else {}

    root = create_root()
    fiber = await root.plugin(resolve_plugin(spec), config)
    try:
        store = root.sessionPersistence
        sid = SessionId("s-redaction")
        await store.create(
            SessionHeader(session_id=sid, created_at=datetime.now(UTC), tenant="acme")
        )
        yield store, sid, tmp_path
    finally:
        await dispose_fiber(fiber)


async def test_a_secret_in_a_message_is_not_in_the_log(created) -> None:
    store, sid, _ = created
    session = await Session.open(store, sid)

    await session.user_message(TurnId(1), f"please deploy with {SECRET} tonight")
    await session.flush()

    events = await _events(store, sid)
    assert SECRET not in _dumped(events)
    assert "ghp_" not in _dumped(events)
    text = next(e.data["text"] for e in events if e.kind == EventKind.USER_MESSAGE)
    assert PLACEHOLDER_PREFIX in text


async def test_the_gate_rewrites_the_event_before_the_model_could_see_it(created) -> None:
    """The model-visible text *is* what the gate wrote — there is no second copy."""
    store, sid, _ = created
    session = await Session.open(store, sid)

    event = await session.user_message(TurnId(1), f"token={SECRET}")

    assert SECRET not in event.data["text"]
    assert event.data["text"].startswith(PLACEHOLDER_PREFIX)
    assert "token=" not in event.data["text"], "at SECRET the payload is withheld, not minimised"


async def test_the_audit_event_names_the_rule_and_carries_no_value(created) -> None:
    store, sid, _ = created
    session = await Session.open(store, sid)

    await session.user_message(TurnId(1), f"the key is {SECRET}")
    await session.flush()

    events = await _events(store, sid)
    audit = [e for e in events if e.kind == EventKind.REDACTION_APPLIED]
    assert len(audit) == 1, "one redaction, one audit record"
    data = audit[0].data
    assert data["event"] == 1, "the audit names the seq it was applied to"
    assert [mark["kind"] for mark in data["marks"]] == ["token"]
    assert all(len(fp) == 16 for fp in data["marks"][0]["fingerprints"])
    assert SECRET not in json.dumps(data, ensure_ascii=False)


async def test_an_ordinary_event_is_written_through_untouched(created) -> None:
    """The gate must be invisible on the 99.9% of events that hold nothing."""
    store, sid, _ = created
    session = await Session.open(store, sid)

    payload = {"turn": 1, "state": "READY", "max_tokens": 200_000, "note": "no secrets here"}
    event = await session.append(EventKind.TASK_STATE, payload)
    await session.flush()

    assert event.data == payload
    events = await _events(store, sid)
    assert [e.kind for e in events] == [EventKind.TASK_STATE]


async def test_a_payload_that_spills_to_a_blob_leaves_no_secret_in_the_blob(created) -> None:
    store, sid, tmp_path = created
    session = await Session.open(store, sid)

    # Well past the inline limit, with the match at the very end — the position a
    # gate at the storage layer would never reach.
    body = ("ordinary build output line\n" * 5000) + f"\nclone https://{INTERNAL_HOST}/team/repo\n"
    await session.append(EventKind.TOOL_RESULT, {"callId": "c1", "content": body, "isError": False})
    await session.flush()

    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    if not any(path.stat().st_size > INLINE_LIMIT for path in files):
        pytest.skip("this store did not spill the payload; the jsonl store does")

    chunks: list[str] = []
    for path in files:
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except PermissionError:  # the store's own lock file, held open by the plugin
            continue
    on_disk = "\n".join(chunks)
    assert INTERNAL_HOST not in on_disk, "the spilled blob kept the intranet URL"
    assert PLACEHOLDER_PREFIX in on_disk
    # …and the descriptor the log kept does not hold it either.
    assert INTERNAL_HOST not in _dumped(await _events(store, sid))


async def test_a_secret_classified_payload_is_withheld_but_keeps_its_shape(created) -> None:
    """At ``SECRET`` the payload goes, not just the matched span.

    A pasted credential is evidence that the *rest* of the payload is sensitive
    too, and the words around a token — a hostname, a project name, an internal
    path — are exactly what a redacted-token placeholder would have preserved.
    Keys and non-string fields survive, because the projection downstream looks
    fields up by name: a payload that lost its shape would break the conversation
    rather than protect it, and would break it one turn later.
    """
    store, sid, _ = created
    session = await Session.open(store, sid)

    event = await session.user_message(TurnId(1), f"deploy with {SECRET} to acme-prod tonight")
    await session.flush()

    assert set(event.data) == {"text"}, "the payload lost its shape"
    assert event.data["text"].startswith(f"{PLACEHOLDER_PREFIX}secret-event:")
    assert "bytes=" in event.data["text"], "the stub must stay diagnosable"
    assert "acme-prod" not in event.data["text"]
    assert "tonight" not in event.data["text"]

    events = await _events(store, sid)
    audit = next(e for e in events if e.kind == EventKind.REDACTION_APPLIED)
    assert audit.data["withheld"] is True
    assert [mark["kind"] for mark in audit.data["marks"]] == ["token"]
    assert SECRET not in _dumped(events)

    later = await session.user_message(TurnId(1), "and the run continues")
    assert later.data["text"] == "and the run continues", "withholding broke the session"


async def test_every_kind_of_event_goes_through_the_gate(created) -> None:
    """D2-1: the gate is on ``append``, so it cannot be kind-specific.

    A rule that lived in one writer would be a rule the next writer forgets. These
    are the three shapes a credential actually arrives in: the model's own text,
    a tool's arguments, and a tool's output.
    """
    store, sid, _ = created
    session = await Session.open(store, sid)

    await session.assistant_message(TurnId(1), 1, f"I will use {SECRET} to publish")
    await session.tool_call(
        TurnId(1),
        2,
        call_id="c1",
        name="run",
        arguments_json=json.dumps({"command": f"export TOKEN={SECRET}"}),
    )
    await session.tool_result(TurnId(1), 3, call_id="c1", content=f"published with {SECRET}")
    await session.flush()

    dumped = _dumped(await _events(store, sid))
    assert SECRET not in dumped
    assert "ghp_" not in dumped


async def test_a_detector_that_raises_degrades_the_data_not_the_run(created) -> None:
    """D2-2: a broken rule must not leak, and must not stop the agent either."""
    store, sid, _ = created

    def boom(match: re.Match[str]) -> str:
        raise ValueError("detector bug")

    rules = (
        Rule(
            name="broken",
            pattern=re.compile(r"(ghp_\w+)"),
            sensitivity=Sensitivity.SECRET,
            rewrite=boom,
            value_group=1,
        ),
    )
    session = await Session.open(store, sid, redactor=Redactor(rules=rules))

    degraded = await session.user_message(TurnId(1), f"deploy with {SECRET}")
    assert SECRET not in degraded.data["text"]
    assert degraded.data["text"].startswith(FAILED_PREFIX)

    later = await session.user_message(TurnId(1), "the run continues")
    assert later.data["text"] == "the run continues", "the failure stopped the session"
    await session.flush()

    events = await _events(store, sid)
    failures = [e for e in events if e.kind == EventKind.REDACTION_FAILED]
    assert len(failures) == 1
    assert failures[0].data["failed"] == "ValueError"
    assert "broken" in failures[0].data["failed_in"]
    assert SECRET not in _dumped(events)


async def test_reopening_a_session_rebuilds_the_redacted_text(created) -> None:
    """M3's invariant, restated honestly: a replay reproduces the *redacted* turn.

    The log never held the original, so no replay can produce it — that is the
    trade D2 accepted, and this is its executable form.
    """
    store, sid, _ = created
    session = await Session.open(store, sid)
    await session.user_message(TurnId(1), f"token={SECRET}")
    await session.flush()

    committed = await _events(store, sid)
    reopened = await Session.open(store, sid)
    again = await reopened.user_message(TurnId(2), "next turn")
    await reopened.flush()

    assert len(committed) < len(await _events(store, sid))
    assert again.seq == len(committed) + 1
    assert SECRET not in _dumped(await _events(store, sid))
    assert any(PLACEHOLDER_PREFIX in (e.data.get("text") or "") for e in committed)
