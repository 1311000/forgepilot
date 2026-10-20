"""Recall: what reaches the prompt, under what heading, and what the log says.

Phase C wrote memories and Phase D made them survive a process. Both of those are
only worth anything if a later turn *sees* them, and this file is about the three
things that can go wrong when it does:

- **The wrong repository's memory.** Recall is scoped to one repository, and the
  scope comes from the caller's authority. A grant that names none, or several, must
  produce no recall at all rather than a plausible-looking guess — so
  ``recaller_for`` refuses, and the refusal is tested here rather than assumed.
- **A line that reads like a fact because it was recalled.** The rendering puts a
  line a person stated or a check proved in one group, a line a tool merely printed
  in another, and a model's unconfirmed proposal in a third, because "it is in the
  log" and "it is true" are not the same claim.
- **A prompt that cannot be rebuilt.** The recall is a ``memory/recalled`` event
  carrying the exact text, so replaying the log reproduces what the model saw even
  after the memory is revoked, expires or is superseded.

The counterfactual for the whole phase is measured, not asserted: with
``recall_for_turn`` returning ``()`` and writing nothing, the end-to-end test below
(the memory from session A appearing in session B's prompt) fails.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from forgepilot.runtime.loop.turn import run_turn
from forgepilot.runtime.projection import derive_messages
from forgepilot.runtime.recall import (
    RECALL_HEADING,
    Recaller,
    recall_for_turn,
    recaller_for,
    render_recalled,
)
from forgepilot.runtime.session import Session
from forgepilot.seams import (
    EventRef,
    ExecutionConstraints,
    MemoryCandidate,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    Mode,
    NetworkAccess,
    Operation,
    Principal,
    PrincipalId,
    Provenance,
    ProvenanceKind,
    RecallQuery,
    RepoRef,
    SessionId,
    TenantId,
)
from forgepilot.seams.agent_loop import UserInput
from forgepilot.seams.refs import TurnId
from forgepilot.seams.session import EventKind, SessionHeader, SurfaceOp

TENANT = TenantId("acme")
OTHER_TENANT = TenantId("other")
REPO = RepoRef(host="git.corp", namespace="team", name="widget")
OTHER_REPO = RepoRef(host="git.corp", namespace="team", name="gadget")

SESSION_A = SessionId("s-recall-a")
SESSION_B = SessionId("s-recall-b")

MEMORY_SPEC = "forgepilot.providers.memory.memory:InMemoryMemoryStorePlugin"
REPLAY_SPEC = "forgepilot.providers.llm.replay:ReplayLLMPlugin"
WATERMARK_SPEC = "forgepilot.providers.distillation.watermark:JsonlWatermarkPlugin"
SESSION_STORE_SPEC = "forgepilot.providers.session_store.memory:InMemorySessionStorePlugin"

#: What the user said in session A. Deliberately phrased as a rule, because that is
#: the kind of memory a human statement produces.
RULE = "the widget build always runs make check before make test"
#: The question session B asks. It shares words with the rule and is *not* a
#: substring of it — the shape the old substring rule could never match.
QUESTION = "why does the check run before the tests in this repository?"


# ---------------------------------------------------------------------------
# the rig
# ---------------------------------------------------------------------------


async def build(tmp_path: Any):
    """A root with a session store, a scripted model, a memory store, watermarks."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root

    root = create_root()
    fibers = []

    async def load(spec: str, cfg: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), cfg or {})
        fibers.append(fiber)
        return fiber

    await load(SESSION_STORE_SPEC)
    await load(REPLAY_SPEC, {"model": "replay-1"})
    await load(MEMORY_SPEC)
    await load(WATERMARK_SPEC, {"path": str(tmp_path / "watermarks.jsonl")})
    return root, fibers


async def close(fibers: Any) -> None:
    from forgepilot.kernel import dispose_fiber

    for fiber in reversed(fibers):
        await dispose_fiber(fiber)


def constraints(
    tenant: TenantId = TENANT, *repos: RepoRef, operations: set[Operation] | None = None
) -> ExecutionConstraints:
    principal = Principal(principal_id=PrincipalId("alice"), tenant_id=tenant)
    return ExecutionConstraints.for_principal(
        principal,
        allowed_repos=frozenset(repos),
        permitted_operations=frozenset(operations or {Operation.READ_CODE}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


def ev(seq: int, session: SessionId = SESSION_A) -> EventRef:
    return EventRef(session_id=session, seq=seq)


def candidate(
    text: str = RULE,
    *,
    subject: str = "widget build order",
    type: MemoryType = MemoryType.COMMAND,  # noqa: A002 - the field's name, not a shadow
    scope_ref: str = REPO.slug,
    tenant: TenantId = TENANT,
) -> MemoryCandidate:
    return MemoryCandidate(
        text=text,
        scope=MemoryScope.REPO,
        scope_ref=scope_ref,
        type=type,
        subject=subject,
        tenant_id=tenant,
    )


def provenance(kind: ProvenanceKind, *events: EventRef, repo: RepoRef | None = REPO) -> Provenance:
    return Provenance(kind=kind, evidence_refs=events, repo=repo)


async def make_session(root: Any, session_id: SessionId, *, tenant: str = "acme") -> Session:
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=session_id,
            created_at=datetime.now(UTC),
            tenant=tenant,
            principal="alice",
        )
    )
    return await Session.open(root.sessionPersistence, session_id)


def kinds(session: Session) -> list[str]:
    return [event.kind for event in session.events]


def recalled(session: Session) -> list[Any]:
    return [event for event in session.events if event.kind == EventKind.MEMORY_RECALLED]


# ---------------------------------------------------------------------------
# which repository is this turn about
# ---------------------------------------------------------------------------


def test_a_caller_who_names_one_repository_gets_one_recaller() -> None:
    class Store:
        pass

    store = Store()
    made = recaller_for(constraints(TENANT, REPO), Mode.TASK, store)

    assert made is not None
    assert made.memory is store
    assert made.scope_ref == REPO.slug
    assert made.mode is Mode.TASK


def test_no_repository_and_several_repositories_both_refuse() -> None:
    """The two refusals that keep a recall from being a guess.

    ``forgepilot --chat`` derives constraints from ``ProjectPolicy.open_project()``,
    which names no repository — so chat has no recall, and that is the honest
    answer rather than a cross-repository read. Several repositories is the same
    problem from the other side: picking one would be choosing which codebase the
    model's answer is about, and only the caller can know.
    """
    store = object()

    assert recaller_for(constraints(TENANT), Mode.CHAT, store) is None
    assert recaller_for(constraints(TENANT, REPO, OTHER_REPO), Mode.TASK, store) is None
    assert recaller_for(constraints(TENANT, REPO), Mode.CHAT, None) is None


# ---------------------------------------------------------------------------
# the rendering: three kinds of claim, kept apart
# ---------------------------------------------------------------------------


async def test_the_rendering_says_what_backs_each_line(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        stored = await root.memory.write(
            candidate(), provenance(ProvenanceKind.HUMAN_VERIFIED, ev(3))
        )
        observed = await root.memory.write(
            candidate("the widget target takes 40 seconds", subject="widget timing"),
            provenance(ProvenanceKind.TOOL_OBSERVED, ev(9)),
        )
        guessed = await root.memory.admit_provisional(
            candidate("the flakiness is a timing bug", subject="flakiness cause"),
            provenance(ProvenanceKind.MODEL_ASSERTED, ev(11)),
        )
        records = tuple(
            await root.memory.recall(
                RecallQuery(text="", scope=MemoryScope.REPO, scope_ref=REPO.slug, limit=10),
                constraints(TENANT, REPO),
            )
        )
        order = {str(record.id): record for record in records}
        body = render_recalled([order[str(stored)], order[str(observed)], order[str(guessed)]])

        assert body.startswith(RECALL_HEADING)
        assert "**not** part of" in body
        assert "the code may have changed" in body

        backed = body.index("### Backed by evidence")
        seen = body.index("### Observed, but not confirmed")
        unconfirmed = body.index("### Unconfirmed")
        assert backed < seen < unconfirmed, "the groups must not be blended"

        assert f"(a person said so, {datetime.now(UTC).date().isoformat()}) {RULE}" in body
        assert "(a tool saw it," in body
        assert "the widget target takes 40 seconds" in body
        assert "(a model proposed it," in body
        assert "Do not skip a check" in body

        # The old wording, which said "verified" over every line including the
        # tool's and the model's. Its absence is the point of the three headings.
        assert "Recalled from previous work (verified)" not in body
    finally:
        await close(fibers)


def test_nothing_to_recall_renders_nothing() -> None:
    assert render_recalled(()) == ""


# ---------------------------------------------------------------------------
# the recall, and the log
# ---------------------------------------------------------------------------


async def test_a_turn_carries_the_memory_and_the_log_says_so(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        memory_id = await root.memory.write(
            candidate(), provenance(ProvenanceKind.HUMAN_VERIFIED, ev(3))
        )
        session = await make_session(root, SESSION_B)
        root.llm.script("the build runs its check first because it is a precondition")

        result = await run_turn(
            session=session,
            user_input=UserInput(text=QUESTION),
            llm=root.llm,
            tools=None,  # nothing is invoked: the scripted turn asks for no tool
            constraints=constraints(TENANT, REPO),
            model="replay-1",
            recaller=recaller_for(constraints(TENANT, REPO), Mode.CHAT, root.memory),
        )

        assert result.reason == "completed"

        # One event, holding the query, the id and the exact text.
        marks = recalled(session)
        assert len(marks) == 1
        assert marks[0].data["query"] == QUESTION
        assert marks[0].data["ids"] == [str(memory_id)]
        assert marks[0].data["scopeRef"] == REPO.slug
        assert marks[0].data["count"] == 1
        assert RULE in marks[0].data["text"]

        # And the model really saw it: the request the runtime sent carries the
        # block, as a *user* message, in place after the question.
        sent = root.llm.requests[-1]
        rendered = [message.content or "" for message in sent.messages]
        assert any(RECALL_HEADING in body for body in rendered)
        assert any(RULE in body for body in rendered)

        # Replaying the log rebuilds the same block — this, not the store, is what
        # "what the model saw" means.
        replayed = [message.content or "" for message in derive_messages(session.events)]
        assert any(RULE in body for body in replayed)
    finally:
        await close(fibers)


async def test_recall_costs_no_extra_model_call(tmp_path: Any) -> None:
    """The claim that recall is a lookup, not a summarisation step.

    Asserted on the request count rather than on a token estimate: a second call
    would be visible here whatever it cost.
    """
    root, fibers = await build(tmp_path)
    try:
        await root.memory.write(candidate(), provenance(ProvenanceKind.HUMAN_VERIFIED, ev(3)))
        session = await make_session(root, SESSION_B)
        root.llm.script("answer")
        before = len(root.llm.requests)

        await run_turn(
            session=session,
            user_input=UserInput(text=QUESTION),
            llm=root.llm,
            tools=None,
            constraints=constraints(TENANT, REPO),
            model="replay-1",
            recaller=recaller_for(constraints(TENANT, REPO), Mode.CHAT, root.memory),
        )

        assert len(root.llm.requests) - before == 1
        assert root.llm.remaining == 0
    finally:
        await close(fibers)


async def test_a_turn_with_nothing_to_recall_writes_no_event(tmp_path: Any) -> None:
    """An empty recall is silent, so "nothing here" and "not wired" stay distinct."""
    root, fibers = await build(tmp_path)
    try:
        session = await make_session(root, SESSION_B)
        root.llm.script("answer")

        await run_turn(
            session=session,
            user_input=UserInput(text=QUESTION),
            llm=root.llm,
            tools=None,
            constraints=constraints(TENANT, REPO),
            model="replay-1",
            recaller=recaller_for(constraints(TENANT, REPO), Mode.CHAT, root.memory),
        )

        assert recalled(session) == []
        assert EventKind.MEMORY_RECALLED not in kinds(session)
    finally:
        await close(fibers)


async def test_a_turn_with_no_recaller_writes_no_event(tmp_path: Any) -> None:
    """``--chat``'s path: no repository in the grant, so no recall and no event."""
    root, fibers = await build(tmp_path)
    try:
        await root.memory.write(candidate(), provenance(ProvenanceKind.HUMAN_VERIFIED, ev(3)))
        session = await make_session(root, SESSION_B)
        root.llm.script("answer")

        await run_turn(
            session=session,
            user_input=UserInput(text=QUESTION),
            llm=root.llm,
            tools=None,
            constraints=constraints(TENANT),
            model="replay-1",
            recaller=recaller_for(constraints(TENANT), Mode.CHAT, root.memory),
        )

        assert recalled(session) == []
    finally:
        await close(fibers)


# ---------------------------------------------------------------------------
# the filters, each one a way a memory must NOT arrive
# ---------------------------------------------------------------------------


async def test_another_tenants_memory_is_not_recalled(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        await root.memory.write(
            candidate(tenant=OTHER_TENANT), provenance(ProvenanceKind.HUMAN_VERIFIED, ev(3))
        )
        session = await make_session(root, SESSION_B)
        root.llm.script("answer")

        await run_turn(
            session=session,
            user_input=UserInput(text=QUESTION),
            llm=root.llm,
            tools=None,
            constraints=constraints(TENANT, REPO),
            model="replay-1",
            recaller=recaller_for(constraints(TENANT, REPO), Mode.CHAT, root.memory),
        )

        assert recalled(session) == []
    finally:
        await close(fibers)


async def test_a_memory_scoped_to_another_repository_is_not_recalled(tmp_path: Any) -> None:
    root, fibers = await build(tmp_path)
    try:
        await root.memory.write(
            candidate(scope_ref=OTHER_REPO.slug),
            provenance(ProvenanceKind.HUMAN_VERIFIED, ev(3), repo=OTHER_REPO),
        )
        session = await make_session(root, SESSION_B)
        root.llm.script("answer")

        await run_turn(
            session=session,
            user_input=UserInput(text=QUESTION),
            llm=root.llm,
            tools=None,
            constraints=constraints(TENANT, REPO),
            model="replay-1",
            recaller=recaller_for(constraints(TENANT, REPO), Mode.CHAT, root.memory),
        )

        assert recalled(session) == []
    finally:
        await close(fibers)


async def test_the_mode_decides_which_types_are_asked_for(tmp_path: Any) -> None:
    """A task summary is for task mode; a chat question does not need one.

    The filter is a *render* filter, not a second copy of the store's rules: the
    store still decides who may read what, and this only decides what is worth
    saying to a turn of this shape.
    """
    root, fibers = await build(tmp_path)
    try:
        await root.memory.write(
            candidate(
                "the widget build order was investigated in TASK-9",
                subject="widget build investigation",
                type=MemoryType.TASK_SUMMARY,
            ),
            provenance(ProvenanceKind.HUMAN_VERIFIED, ev(3)),
        )
        constraints_acme = constraints(TENANT, REPO)
        session = await make_session(root, SESSION_B)
        store = root.memory
        query_turn = UserInput(text=QUESTION)

        chat = await recall_for_turn(
            session,
            recaller=Recaller(memory=store, scope_ref=REPO.slug, mode=Mode.CHAT),
            text=query_turn.text,
            constraints=constraints_acme,
            turn=session.next_turn(),
        )
        assert chat == ()
        assert recalled(session) == []

        task = await recall_for_turn(
            session,
            recaller=Recaller(memory=store, scope_ref=REPO.slug, mode=Mode.TASK),
            text=query_turn.text,
            constraints=constraints_acme,
            turn=session.next_turn(),
        )
        assert len(task) == 1
        assert len(recalled(session)) == 1
    finally:
        await close(fibers)


# ---------------------------------------------------------------------------
# the end-to-end the phase exists for
# ---------------------------------------------------------------------------


async def test_memory_from_session_a_reaches_session_b_s_prompt(tmp_path: Any) -> None:
    """Distil session A, then ask session B a question — and B knows.

    This is acceptance row §八-1 in one test: the memory is written by distillation
    (not by hand), it is `TEST_VERIFIED`/`HIGH` because the cited event is a passing
    check, session B's prompt carries it, and the log holds both `memory/write` and
    `memory/recalled`.

    Counterfactual, measured: make ``recall_for_turn`` return ``()`` without writing
    the event and this test fails on the prompt assertion — which is what makes it a
    test of recall rather than a test of the store.
    """
    from forgepilot.runtime.distillation import distill

    root, fibers = await build(tmp_path)
    try:
        session_a = await make_session(root, SESSION_A)
        await session_a.turn_start(TurnId(1))
        said = await session_a.user_message(TurnId(1), "the widget build always runs make check")
        await session_a.turn_end(TurnId(1), "completed")
        await session_a.turn_start(TurnId(2))
        await session_a.append(
            EventKind.TASK_CHECK,
            {
                "passed": True,
                "command": "make check",
                "commands": ["make check"],
                "exitCode": 0,
                "output": "ok",
                "couldNotRun": False,
                "reason": "",
                "meta": {},
            },
            surface=SurfaceOp.APPEND,
        )
        await session_a.turn_end(TurnId(2), "completed")
        await session_a.flush()

        root.llm.script(
            json.dumps(
                [
                    {
                        "type": "command",
                        "subject": "widget build order",
                        "text": RULE,
                        "evidence_seqs": [said.seq],
                        "model_confidence_hint": 0.9,
                    }
                ]
            )
        )
        report = await distill(
            session_a,
            llm=root.llm,
            memory=root.memory,
            watermarks=root.distillationWatermarks,
            scope_ref=REPO.slug,
            tenant_id="acme",
            repo=REPO,
            model="replay-1",
        )
        assert report.written == 1 and report.failed == ""
        memory_id = report.memories[0]

        stored = await root.memory.recall(
            RecallQuery(text="", scope=MemoryScope.REPO, scope_ref=REPO.slug),
            constraints(TENANT, REPO),
        )
        assert [str(record.id) for record in stored] == [memory_id]
        assert stored[0].provenance.kind is ProvenanceKind.HUMAN_VERIFIED
        assert stored[0].status is MemoryStatus.ACTIVE

        # A different session, in the same store: "session B" — and by the time
        # Phase D is deployed the store is a file, so process identity is not what
        # makes this a different session, the session id is.
        session_b = await make_session(root, SESSION_B)
        root.llm.script("because make check is a precondition of the test target")
        await run_turn(
            session=session_b,
            user_input=UserInput(text=QUESTION),
            llm=root.llm,
            tools=None,
            constraints=constraints(TENANT, REPO),
            model="replay-1",
            recaller=recaller_for(constraints(TENANT, REPO), Mode.CHAT, root.memory),
        )

        sent = root.llm.requests[-1]
        assert any(RULE in (message.content or "") for message in sent.messages)
        assert EventKind.MEMORY_WRITE in kinds(session_a)
        assert EventKind.MEMORY_RECALLED in kinds(session_b)
        assert [event.data["ids"] for event in recalled(session_b)] == [[memory_id]]
    finally:
        await close(fibers)


async def test_the_recalled_text_survives_the_memory_being_forgotten(tmp_path: Any) -> None:
    """The log's copy is the record, not a pointer into a store that can change.

    Revoking a memory must stop it being recalled from now on; it must not
    retroactively rewrite the prompt the model was shown last week.
    """
    root, fibers = await build(tmp_path)
    try:
        memory_id = await root.memory.write(
            candidate(), provenance(ProvenanceKind.HUMAN_VERIFIED, ev(3))
        )
        session = await make_session(root, SESSION_B)
        root.llm.script("answer")
        await run_turn(
            session=session,
            user_input=UserInput(text=QUESTION),
            llm=root.llm,
            tools=None,
            constraints=constraints(TENANT, REPO),
            model="replay-1",
            recaller=recaller_for(constraints(TENANT, REPO), Mode.CHAT, root.memory),
        )
        assert RULE in recalled(session)[0].data["text"]

        await root.memory.revoke(memory_id)
        assert recalled(session)[0].data["text"].count(RULE) == 1
        replayed = [message.content or "" for message in derive_messages(session.events)]
        assert any(RULE in body for body in replayed)
    finally:
        await close(fibers)


@pytest.mark.parametrize("kind", [ProvenanceKind.TOOL_OBSERVED, ProvenanceKind.TEST_VERIFIED])
async def test_every_kind_of_backing_produces_a_line(tmp_path: Any, kind: ProvenanceKind) -> None:
    """No backing kind is silently dropped by the renderer's grouping."""
    root, fibers = await build(tmp_path)
    try:
        await root.memory.write(candidate(), provenance(kind, ev(3), ev(4)))
        session = await make_session(root, SESSION_B)
        records = await recall_for_turn(
            session,
            recaller=Recaller(memory=root.memory, scope_ref=REPO.slug, mode=Mode.CHAT),
            text=QUESTION,
            constraints=constraints(TENANT, REPO),
            turn=session.next_turn(),
        )
        assert len(records) == 1
        body = recalled(session)[0].data["text"]
        assert RULE in body
    finally:
        await close(fibers)
