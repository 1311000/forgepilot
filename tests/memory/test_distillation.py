"""Distillation: what the model may claim, and what the harness decides.

The model here is scripted (`ReplayLLMPlugin`), deliberately: the interesting
behaviour is not extraction quality but what happens to an extraction afterwards.
A scripted answer lets each property be tested against a citation that is right, a
citation that is *invented*, and a citation that proves nothing — and those three
are the whole of this milestone's safety argument.

Four properties are load-bearing, and each has a counterfactual written next to it:

- **Evidence or nothing.** A candidate citing a seq this run did not show the model
  is dropped. Counterfactual: remove the citation check and a model that types 9999
  lands `ACTIVE`/`HIGH`.
- **Reading is not believing.** A cited `read_file` result yields a `PROPOSED`
  memory, never a verified one.
- **The watermark, not the hash, is what stops a second extraction.** Counterfactual:
  without it the same stretch is paid for twice; the store's content hash only makes
  the second copy harmless.
- **A failed run writes nothing and does not advance.** So the stretch stays
  available for a retry with a larger cap instead of being lost.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from forgepilot.providers.llm.replay import text_turn
from forgepilot.runtime.distillation import distill, evidence_digest, window_for
from forgepilot.runtime.session import Session
from forgepilot.seams.distillation import DistillationSchemaError, parse_candidates
from forgepilot.seams.llm import Usage
from forgepilot.seams.memory import Confidence, MemoryStatus, ProvenanceKind
from forgepilot.seams.refs import RepoRef, SessionId, TurnId
from forgepilot.seams.security import Budget
from forgepilot.seams.session import EventKind, SessionHeader, SurfaceOp

TENANT = "acme"
REPO = RepoRef(host="git.corp", namespace="team", name="widget")
SCOPE_REF = REPO.slug
SESSION = SessionId("s-distil")

WATERMARK_SPEC = "forgepilot.providers.distillation.watermark:JsonlWatermarkPlugin"
MEMORY_SPEC = "forgepilot.providers.memory.memory:InMemoryMemoryStorePlugin"
REPLAY_SPEC = "forgepilot.providers.llm.replay:ReplayLLMPlugin"


# ---------------------------------------------------------------------------
# the rig
# ---------------------------------------------------------------------------


async def build(tmp_path: Any, *, tenant: str = TENANT):
    """A root with a session store, a scripted model, a memory store and watermarks."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root

    root = create_root()
    fibers = []

    async def load(spec: str, cfg: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), cfg or {})
        fibers.append(fiber)
        return fiber

    await load("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin")
    await load(REPLAY_SPEC, {"model": "replay-1"})
    await load(MEMORY_SPEC)
    await load(WATERMARK_SPEC, {"path": str(tmp_path / "watermarks.jsonl")})
    return root, fibers


async def close(fibers: Any) -> None:
    from forgepilot.kernel import dispose_fiber

    for fiber in reversed(fibers):
        await dispose_fiber(fiber)


async def make_session(
    root: Any, *, tenant: str = TENANT, could_not_run: bool = False
) -> tuple[Session, dict[str, int]]:
    """One finished session: a user's rule, a check that ran, a run, and a read.

    Returns the session and the seq numbers a candidate could cite — the point of
    returning them is that no test hardcodes a seq, so inserting an event cannot
    silently make a citation mean something else.
    """
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=SESSION,
            created_at=datetime.now(UTC),
            tenant=tenant,
            principal="alice",
        )
    )
    session = await Session.open(root.sessionPersistence, SESSION)

    await session.turn_start(TurnId(1))
    user = await session.user_message(
        TurnId(1), "we never edit anything under vendor/, it is generated"
    )
    await session.turn_end(TurnId(1), "completed")

    await session.turn_start(TurnId(2))
    await session.assistant_message(
        TurnId(2),
        1,
        "",
        tool_calls=[{"callId": "c1", "name": "run", "arguments": "{}"}],
    )
    call = await session.tool_call(
        TurnId(2), 1, call_id="c1", name="run", arguments_json='{"command": "pytest -q"}'
    )
    ran = await session.tool_result(TurnId(2), 1, call_id="c1", content="8 passed in 0.4s")
    check = await session.append(
        EventKind.TASK_CHECK,
        {
            "passed": not could_not_run,
            "command": "pytest -q",
            "commands": ["pytest -q"],
            "exitCode": 1 if could_not_run else 0,
            "output": "8 passed",
            "couldNotRun": could_not_run,
            "reason": "the runner is missing" if could_not_run else "",
            "meta": {},
        },
        surface=SurfaceOp.APPEND,
    )
    await session.turn_end(TurnId(2), "completed")

    await session.turn_start(TurnId(3))
    await session.assistant_message(
        TurnId(3),
        1,
        "",
        tool_calls=[{"callId": "c2", "name": "read_file", "arguments": "{}"}],
    )
    await session.tool_call(
        TurnId(3), 1, call_id="c2", name="read_file", arguments_json='{"path": "vendor/x.py"}'
    )
    read = await session.tool_result(
        TurnId(3), 1, call_id="c2", content="the vendor directory is generated by make"
    )
    end = await session.turn_end(TurnId(3), "completed")

    await session.flush()
    return session, {
        "user": user.seq,
        "call": call.seq,
        "ran": ran.seq,
        "check": check.seq,
        "read": read.seq,
        # The watermark moves to the last *closed turn* (14), not to the last event a
        # candidate could cite (13): the former is what "this stretch is finished"
        # means, and an open turn's events must stay available for the next pass.
        "upto": end.seq,
    }


def answer(*candidates: dict[str, Any]) -> str:
    return json.dumps(list(candidates))


def candidate(
    seqs: list[int],
    *,
    type: str = "preference",  # noqa: A002 — the schema's field name, not a shadow
    subject: str = "vendor-directory",
    text: str = "Nothing under vendor/ is edited by hand; it is generated.",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": type,
        "subject": subject,
        "text": text,
        "evidence_seqs": list(seqs),
        **extra,
    }


async def run(root: Any, session: Session, **kwargs: Any):
    """One distillation pass, the way the CLI runs it."""
    options: dict[str, Any] = {
        "scope_ref": SCOPE_REF,
        "repo": REPO,
        "budget": kwargs.pop("budget", None),
        "since": kwargs.pop("since", None),
        "dry_run": kwargs.pop("dry_run", False),
    }
    options.update(kwargs)
    return await distill(
        session,
        llm=root.llm,
        memory=root.memory,
        watermarks=root.distillationWatermarks,
        **options,
    )


def events(session: Session, kind: str) -> list[Any]:
    return [event for event in session.events if event.kind == kind]


def kinds(session: Session) -> list[str]:
    return [event.kind for event in session.events]


# ---------------------------------------------------------------------------
# the schema: the model fills in five fields, and cannot reach the others
# ---------------------------------------------------------------------------


def test_a_fenced_answer_parses_like_a_bare_one() -> None:
    body = json.dumps([candidate([3])])
    assert parse_candidates(f"```json\n{body}\n```") == parse_candidates(body)


def test_an_extra_field_is_refused_rather_than_ignored() -> None:
    """The refusal is the mechanism, not pedantry.

    A model that volunteers `confidence` is answering a question it was not asked;
    dropping the field quietly would leave the next reader believing the model's
    number was considered and rejected on its merits.
    """
    with pytest.raises(DistillationSchemaError, match="confidence"):
        parse_candidates(json.dumps([candidate([3], confidence=0.99)]))
    with pytest.raises(DistillationSchemaError, match="status"):
        parse_candidates(json.dumps([candidate([3], status="active")]))


def test_a_candidate_must_name_a_type_this_build_knows() -> None:
    with pytest.raises(DistillationSchemaError, match="musing"):
        parse_candidates(json.dumps([candidate([3], type="musing")]))


def test_too_many_candidates_is_refused_rather_than_truncated() -> None:
    many = [candidate([3], subject=f"subject-{n}") for n in range(5)]
    with pytest.raises(DistillationSchemaError, match="more than the 4"):
        parse_candidates(json.dumps(many), limit=4)


def test_a_hint_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(DistillationSchemaError, match="outside 0..1"):
        parse_candidates(json.dumps([candidate([3], model_confidence_hint=1.5)]))
    assert (
        parse_candidates(json.dumps([candidate([3], model_confidence_hint=0.5)]))[
            0
        ].model_confidence_hint
        == 0.5
    )


def test_a_boolean_where_a_sequence_number_belongs_is_refused() -> None:
    with pytest.raises(DistillationSchemaError, match="sequence number"):
        parse_candidates(json.dumps([candidate([True])]))


# ---------------------------------------------------------------------------
# the window and the digest
# ---------------------------------------------------------------------------


def make_events(*specs: tuple[str, int, datetime | None]) -> list[Any]:
    from forgepilot.seams.session import SessionEvent

    return [
        SessionEvent(seq=seq, kind=kind, data={}, surface=SurfaceOp.APPEND, ts=ts)
        for kind, seq, ts in specs
    ]


def test_the_window_starts_after_the_watermark_and_stops_at_the_last_closed_turn() -> None:
    log = make_events(
        (EventKind.USER_MESSAGE, 1, None),
        (EventKind.TURN_END, 2, None),
        (EventKind.USER_MESSAGE, 3, None),
        (EventKind.TURN_END, 4, None),
        (EventKind.USER_MESSAGE, 5, None),  # a turn that has not closed
    )
    window = window_for(log, watermark=2, upto=4)
    assert [event.seq for event in window] == [3, 4]


def test_an_event_with_no_timestamp_is_kept_when_since_is_given() -> None:
    """Dropping evidence because a clock field is missing is how a memory loses
    the thing that justified it."""
    old = datetime.now(UTC) - timedelta(days=2)
    log = make_events(
        (EventKind.USER_MESSAGE, 1, old),
        (EventKind.USER_MESSAGE, 2, None),
        (EventKind.USER_MESSAGE, 3, datetime.now(UTC)),
    )
    window = window_for(log, watermark=0, upto=9, since=datetime.now(UTC) - timedelta(hours=1))
    assert [event.seq for event in window] == [2, 3]


async def test_the_digest_lists_only_what_may_be_cited(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        digest = evidence_digest(session.events)
    finally:
        await close(fibers)

    assert f"{seqs['user']} user:" in digest
    assert f"{seqs['check']} check [passed]: pytest -q" in digest
    assert f"{seqs['ran']} tool result:" in digest
    # A log-only event exists in the log and is never citable, so it is not listed
    # either — the digest is a map of what *will be accepted*.
    assert f"{seqs['call']} " not in digest
    call_event = next(e for e in session.events if e.seq == seqs["call"])
    assert call_event.is_model_visible is False
    assert "only seq numbers that will be accepted" in digest


# ---------------------------------------------------------------------------
# the procedure
# ---------------------------------------------------------------------------


async def test_a_cited_user_message_becomes_a_verified_memory(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        root.llm.script(answer(candidate([seqs["user"]])))
        report = await run(root, session)

        assert report.ok and report.candidates == 1
        assert report.written == 1 and report.provisional == 0
        assert report.watermark_after == seqs["upto"]
        assert len(root.memory.records) == 1
        record = root.memory.records[0]
        assert record.status is MemoryStatus.ACTIVE
        assert record.confidence is Confidence.HIGH
        assert record.provenance.kind is ProvenanceKind.HUMAN_VERIFIED
        assert [str(ref) for ref in record.provenance.evidence_refs] == [
            f"{SESSION}#{seqs['user']}"
        ]
        assert record.tenant_id == TENANT
        assert record.candidate.scope_ref == SCOPE_REF
    finally:
        await close(fibers)


async def test_a_cited_passing_check_is_verified(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        root.llm.script(answer(candidate([seqs["check"]], type="convention")))
        report = await run(root, session)
        assert report.written == 1
        assert root.memory.records[0].provenance.kind is ProvenanceKind.TEST_VERIFIED
    finally:
        await close(fibers)


async def test_a_check_that_could_not_run_is_not_evidence(tmp_path: Any) -> None:
    """A broken runner is not a passing test, and it must not be able to look like one.

    This gets its own rig on purpose: the same claim written twice under one tenant
    and scope is one record (the content hash ignores the session id), so asserting
    the *witness* on a store that already holds the claim would be asserting on a
    de-duplication path instead.
    """
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root, could_not_run=True)
        root.llm.script(answer(candidate([seqs["check"]], type="convention")))
        report = await run(root, session)
        assert report.written == 0 and report.provisional == 1
        assert root.memory.records[-1].status is MemoryStatus.PROPOSED
        write = events(session, EventKind.MEMORY_WRITE)[0]
        assert write.data["status"] == "proposed"
        assert "could not run" in write.data["reason"]
    finally:
        await close(fibers)


async def test_a_cited_read_is_filed_as_a_proposal(tmp_path: Any) -> None:
    """Reading that something is so is not evidence that it is so.

    Otherwise a hostile file would launder its contents into memory, which is the
    "a check that cannot fail" defect one indirection further out.
    """
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        root.llm.script(answer(candidate([seqs["read"]], type="fact", subject="vendor-dir")))
        report = await run(root, session)

        assert report.written == 0 and report.provisional == 1
        record = root.memory.records[0]
        assert record.status is MemoryStatus.PROPOSED
        assert record.confidence is Confidence.LOW
        assert record.provenance.kind is ProvenanceKind.MODEL_ASSERTED
        # The citation is kept: the memory stays traceable to what was read.
        assert [str(ref) for ref in record.provenance.evidence_refs] == [
            f"{SESSION}#{seqs['read']}"
        ]
        write = events(session, EventKind.MEMORY_WRITE)[0]
        assert write.data["status"] == "proposed"
        assert "reads" in write.data["reason"]
    finally:
        await close(fibers)


async def test_an_invented_sequence_is_dropped_rather_than_believed(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        root.llm.script(answer(candidate([seqs["user"], 9999])))
        report = await run(root, session)

        assert report.written == 0 and report.dropped == 1
        assert not root.memory.records
        assert any("9999" in note for note in report.notes)
        # And the watermark still advances: retrying a stretch whose citations were
        # bad would fail the same way forever, which is a stall, not safety.
        assert report.watermark_after == seqs["upto"]
        assert await root.distillationWatermarks.get(str(SESSION)) == seqs["upto"]
    finally:
        await close(fibers)


async def test_a_log_only_event_may_not_be_cited(tmp_path: Any) -> None:
    """The citation rule is "what this run showed the model", not "what the log has"."""
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        root.llm.script(answer(candidate([seqs["call"]])))
        report = await run(root, session)

        assert report.dropped == 1 and report.written == 0
        assert any(str(seqs["call"]) in note for note in report.notes)
    finally:
        await close(fibers)


async def test_a_second_pass_over_the_same_stretch_finds_nothing_to_do(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        root.llm.script(answer(candidate([seqs["user"]])))
        first = await run(root, session)

        before = len(session.events)
        second = await run(root, session)

        assert second.events == 0 and second.candidates == 0
        assert second.written == 0 and second.watermark_after == first.watermark_after
        assert "nothing to distil" in second.notes[0]
        # No second run happened: not even a start event. A run that did nothing
        # has nothing to audit, and a `distillation/start` would claim otherwise.
        assert len(session.events) == before
        assert len(root.memory.records) == 1
    finally:
        await close(fibers)


async def test_without_the_watermark_the_stretch_is_extracted_again_but_not_duplicated(
    tmp_path: Any,
) -> None:
    """The counterfactual for the watermark, and what the content hash really buys.

    A fresh watermark file makes the same stretch look new. The extraction runs
    again — that is the cost the watermark exists to prevent — and the store's
    content hash is what stops it from producing a second memory. Both facts are
    asserted, because "it did not duplicate" alone would leave the watermark
    looking unnecessary.
    """
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root, fibers = await build(tmp_path)
    other = create_root()
    fiber = await other.plugin(
        resolve_plugin(WATERMARK_SPEC), {"path": str(tmp_path / "fresh.jsonl")}
    )
    try:
        session, seqs = await make_session(root)
        root.llm.script(answer(candidate([seqs["user"]])))
        first = await run(root, session)

        root.llm.script(answer(candidate([seqs["user"]])))
        second = await distill(
            session,
            llm=root.llm,
            memory=root.memory,
            watermarks=other.distillationWatermarks,
            scope_ref=SCOPE_REF,
            repo=REPO,
        )

        assert second.candidates == 1 and second.events == first.events
        assert second.memories == first.memories
        assert len(root.memory.records) == 1
        assert await other.distillationWatermarks.get(str(SESSION)) == seqs["upto"]
    finally:
        await dispose_fiber(fiber)
        await close(fibers)


async def test_an_answer_that_is_not_the_schema_fails_the_run_and_keeps_the_answer(
    tmp_path: Any,
) -> None:
    root, fibers = await build(tmp_path)
    try:
        session, _ = await make_session(root)
        raw = "I think the team probably prefers tabs, but I am not sure."
        root.llm.script(raw)
        report = await run(root, session)

        assert report.failed == "schema" and report.detail
        assert report.written == 0 and not root.memory.records
        assert report.watermark_after == 0
        assert await root.distillationWatermarks.get(str(SESSION)) == 0
        # The raw answer is kept even though it could not be used: it is the only
        # way to tell a model that rambles from a prompt that asks for the wrong thing.
        summary = events(session, EventKind.DISTILLATION_SUMMARY)[0]
        assert summary.data["text"] == raw
        failed = events(session, EventKind.DISTILLATION_FAILED)[0]
        assert failed.data["code"] == "schema"
        assert kinds(session)[-1] == EventKind.DISTILLATION_END
    finally:
        await close(fibers)


async def test_a_budget_overrun_writes_nothing_and_leaves_the_stretch_for_a_retry(
    tmp_path: Any,
) -> None:
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        root.llm.script(
            text_turn(
                answer(candidate([seqs["user"]])),
                usage=Usage(input_tokens=900, output_tokens=100, cost_usd=0.02),
            )
        )
        report = await run(root, session, budget=Budget(max_tokens=500))

        assert report.failed == "budget"
        assert report.written == 0 and not root.memory.records
        assert report.input_tokens == 900 and report.output_tokens == 100
        assert await root.distillationWatermarks.get(str(SESSION)) == 0
        assert events(session, EventKind.DISTILLATION_FAILED)[0].data["code"] == "budget"

        # The retry sees the same stretch, which is the point of not advancing.
        root.llm.script(answer(candidate([seqs["user"]])))
        retry = await run(root, session, budget=Budget(max_tokens=5_000))
        assert retry.ok and retry.written == 1
        assert len(root.memory.records) == 1
    finally:
        await close(fibers)


async def test_a_dry_run_records_the_run_and_writes_no_memory(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        root.llm.script(answer(candidate([seqs["user"]])))
        report = await run(root, session, dry_run=True)

        assert report.candidates == 1 and report.dry_run
        assert report.written == 0 and report.provisional == 0
        assert not root.memory.records
        assert await root.distillationWatermarks.get(str(SESSION)) == 0
        assert report.watermark_after == report.watermark_before == 0
        assert "would file" in " ".join(report.notes)
        # The run is still auditable — it really called the model.
        assert kinds(session)[-1] == EventKind.DISTILLATION_END
        assert events(session, EventKind.DISTILLATION_REQUEST)
        assert events(session, EventKind.MEMORY_WRITE) == []
    finally:
        await close(fibers)


async def test_memory_write_names_its_evidence_and_the_harnesss_reason(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        root.llm.script(answer(candidate([seqs["user"]])))
        report = await run(root, session)

        write = events(session, EventKind.MEMORY_WRITE)[0]
        data = write.data
        assert data["memoryId"] == report.memories[0]
        assert data["status"] == "active"
        assert data["kind"] == str(ProvenanceKind.HUMAN_VERIFIED)
        assert data["confidence"] == str(Confidence.HIGH)
        assert data["evidenceSeqs"] == [f"{SESSION}#{seqs['user']}"]
        assert data["reason"]
        assert data["scopeRef"] == SCOPE_REF and data["tenant"] == TENANT
        # The text is in the log on purpose: a memory has to be reconstructable and
        # auditable from the record of how it was made. It was redacted on the way in.
        assert data["text"].startswith("Nothing under vendor/")
        assert write.surface is SurfaceOp.LOG_ONLY
    finally:
        await close(fibers)


async def test_the_run_is_reconstructable_from_the_log_alone(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        payload = answer(candidate([seqs["user"]]))
        root.llm.script(payload)
        report = await run(root, session)

        assert kinds(session)[-5:] == [
            EventKind.DISTILLATION_START,
            EventKind.DISTILLATION_REQUEST,
            EventKind.DISTILLATION_SUMMARY,
            EventKind.MEMORY_WRITE,
            EventKind.DISTILLATION_END,
        ]
        start = events(session, EventKind.DISTILLATION_START)[0].data
        assert start["watermark"] == 0 and start["upto"] == seqs["upto"]
        assert start["events"] == report.events and start["turns"] == 3
        request = events(session, EventKind.DISTILLATION_REQUEST)[0].data
        assert request["model"] == "replay-1"
        assert request["region"] == [1, seqs["upto"]]
        # Two system sections: the instruction and the citation digest. The digest
        # is an instruction about the answer, so it is not among the messages.
        assert request["maxTokens"] == 4096 and request["systemSections"] == 2
        assert request["watermark"] == 0 and request["upto"] == seqs["upto"]
        end = events(session, EventKind.DISTILLATION_END)[0].data
        assert end["written"] == 1 and end["memories"] == [report.memories[0]]
        assert end["tokens"] == {"input": 0, "output": 0}
    finally:
        await close(fibers)


async def test_a_session_with_no_tenant_never_starts(tmp_path: Any) -> None:
    """Fail before the model call, not after: the store would refuse every write."""
    root, fibers = await build(tmp_path)
    try:
        session, _ = await make_session(root, tenant="")
        root.llm.script(answer(candidate([1])))
        report = await run(root, session)

        assert report.failed == "no-tenant"
        assert root.llm.requests == []
        assert events(session, EventKind.DISTILLATION_START) == []
        assert report.watermark_after == 0
    finally:
        await close(fibers)


async def test_no_repository_means_no_distillation(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        session, _ = await make_session(root)
        report = await run(root, session, scope_ref="")
        assert report.failed == "no-scope"
        assert root.llm.requests == []
        assert report.watermark_after == 0
    finally:
        await close(fibers)


async def test_a_store_refusal_holds_the_watermark_before_the_claim(tmp_path: Any) -> None:
    """A refusal is a property of the store, so the claim must stay reachable.

    A bad citation is settled: the same window would show the model the same events,
    so retrying would fail identically and advancing past it is honest. "This store
    said no" is not settled — a migration, a tenant change or a scope rule that
    learns an answer can make the same claim admissible tomorrow. Advancing past it
    would drop the claim for good, which is the one failure mode a watermark can
    cause.
    """
    from forgepilot.errors import ForgePilotError

    class Refusing:
        """A store that answers the seam's write with its own refusal."""

        async def write(self, item: Any, provenance: Any) -> Any:
            raise ForgePilotError("this store will not take it, yet")

        async def reconcile(self, memory_id: Any) -> tuple[Any, ...]:
            return ()

    root, fibers = await build(tmp_path)
    try:
        session, seqs = await make_session(root)
        root.llm.script(answer(candidate([seqs["user"]])))

        report = await distill(
            session,
            llm=root.llm,
            memory=Refusing(),
            watermarks=root.distillationWatermarks,
            scope_ref=SCOPE_REF,
            repo=REPO,
        )

        assert report.deferred == 1
        assert report.dropped == 1
        # One before the cited event: everything up to it is settled, and the claim's
        # own stretch is still visible to the next run.
        assert report.watermark_after == seqs["user"] - 1
        assert report.watermark_after < seqs["upto"]
        assert await root.distillationWatermarks.get(str(SESSION)) == report.watermark_after

        ended = events(session, EventKind.DISTILLATION_END)[-1]
        assert ended.data["deferred"] == 1
        assert any("watermark stops" in note for note in report.notes)
    finally:
        await close(fibers)
