"""Forgetting and purging: who is asked, what is written down, and what that costs.

Phase F's store work is measured in ``tests/contract/test_memory_model.py``, against
both providers. This file measures the part that is *not* in a store: the harness's own
acts.

Three failure modes, and each has a test that goes red for it:

1. **A removal nobody authorised.** ``revoke`` and ``purge`` are store methods; calling
   one directly is a decision about what the system believes with no principal behind
   it. The runtime path asks policy *first* — a refusal must leave the store untouched,
   which is the only thing that separates "the check ran" from "the check ran after".
2. **A removal nobody answered.** The shipped `approval` row has no answerer, so an
   ``ask`` resolves to ``UNAVAILABLE`` — which is a refusal everywhere in this system.
   A forget that succeeded there would mean the prompt was decorative.
3. **A purge that left something behind, or a purge that left nothing at all.** The
   content must be gone; the tombstone, and the event that carries it, must not be.

The reconciliation tests at the end are the other half of the plan's §六: a resolution
is written to the log when it happens, and it happens on the distillation path, where
memories actually enter the system — not at recall time, where a model is waiting.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from forgepilot.runtime.memory_lifecycle import forget, purge, record_resolutions
from forgepilot.runtime.session import Session
from forgepilot.seams.approval import ApprovalOutcome
from forgepilot.seams.distillation import Witness
from forgepilot.seams.memory import (
    MemoryScope,
    MemoryStatus,
    ProvenanceKind,
    Resolution,
    ResolutionKind,
)
from forgepilot.seams.refs import MemoryId, PrincipalId, RepoRef, SessionId, TenantId
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal
from forgepilot.seams.session import EventKind, SessionHeader

TENANT = TenantId("acme")
REPO = RepoRef(host="git.corp", namespace="team", name="widget")
MAINTENANCE = SessionId("memory-maintenance-acme")

PRINCIPAL = Principal(principal_id=PrincipalId("alice"), tenant_id=TENANT)

MEMORY_SPEC = "forgepilot.providers.memory.memory:InMemoryMemoryStorePlugin"
SESSION_STORE_SPEC = "forgepilot.providers.session_store.memory:InMemorySessionStorePlugin"
POLICY_SPEC = "forgepilot.identity.policy:PolicyServicePlugin"
APPROVAL_SPEC = "forgepilot.runtime.approval:ApprovalServicePlugin"

#: Ships with the ask list, so both commands stop for a human by default.
SHIPPED_POLICY = {"default": "allow", "ask_operations": ["forget_memory", "purge_memory"]}


# ---------------------------------------------------------------------------
# the rig
# ---------------------------------------------------------------------------


async def build(policy_config: dict | None = None):
    """A root with a log, a memory store, the decision authority and an answerer."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root

    root = create_root()
    fibers: list[Any] = []

    async def load(spec: str, cfg: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), cfg or {})
        fibers.append(fiber)
        return fiber

    await load(SESSION_STORE_SPEC)
    await load(MEMORY_SPEC)
    await load(APPROVAL_SPEC, {"policy": "ask"})
    await load(POLICY_SPEC, policy_config or SHIPPED_POLICY)
    return root, fibers


async def close(fibers: list[Any]) -> None:
    from forgepilot.kernel import dispose_fiber

    for fiber in reversed(fibers):
        await dispose_fiber(fiber)


async def make_session(root: Any, session_id: SessionId = MAINTENANCE) -> Session:
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=session_id,
            created_at=datetime.now(UTC),
            tenant=str(TENANT),
            principal="alice",
        )
    )
    return await Session.open(root.sessionPersistence, session_id)


def answer_with(outcome: ApprovalOutcome):
    async def answerer(request: Any) -> ApprovalOutcome:
        return outcome

    return answerer


def constraints() -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        PRINCIPAL,
        allowed_repos=frozenset({REPO}),
        permitted_operations=frozenset(Operation),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


async def admit(root: Any, text: str = "the widget build runs make check", **overrides: Any):
    """A memory in the store, written the way distillation writes one."""
    from forgepilot.seams.memory import EventRef, MemoryCandidate, MemoryType, Provenance

    candidate = MemoryCandidate(
        text=text,
        scope=MemoryScope.REPO,
        scope_ref=REPO.slug,
        type=overrides.get("type", MemoryType.FACT),
        subject=overrides.get("subject", "widget build order"),
        tenant_id=TENANT,
    )
    provenance = Provenance(
        kind=overrides.get("kind", ProvenanceKind.TEST_VERIFIED),
        evidence_refs=(EventRef(session_id=MAINTENANCE, seq=1),),
        repo=REPO,
    )
    return await root.memory.write(candidate, provenance)


def kinds(session: Session) -> list[str]:
    return [event.kind for event in session.events]


def data_for(session: Session, kind: str) -> dict[str, Any]:
    events = [event for event in session.events if event.kind == kind]
    assert events, f"no {kind} event was written"
    return dict(events[-1].data)


async def recalled(root: Any) -> list[Any]:
    from forgepilot.seams.memory import RecallQuery

    query = RecallQuery(text="", scope=MemoryScope.REPO, scope_ref=REPO.slug)
    return list(await root.memory.recall(query, constraints()))


# ---------------------------------------------------------------------------
# forgetting — reversible, authorised, and recorded
# ---------------------------------------------------------------------------


async def test_forgetting_asks_a_human_and_records_the_answer() -> None:
    root, fibers = await build()
    try:
        session = await make_session(root)
        memory_id = await admit(root)
        root.approval.set_answerer(answer_with(ApprovalOutcome.ALLOWED_ONCE))

        outcome = await forget(
            session,
            memory=root.memory,
            memory_id=memory_id,
            policy=root.policy,
            approval=root.approval,
            principal=PRINCIPAL,
            reason="it was wrong about the build",
        )

        assert outcome.allowed and outcome.asked
        assert await recalled(root) == [], "a forgotten memory is not served"
        record = {record.id: record for record in root.memory.records}[memory_id]
        assert record.status is MemoryStatus.REVOKED
        assert record.candidate.text == "the widget build runs make check", (
            "forgetting keeps the evidence: this is what makes it reversible"
        )
        payload = data_for(session, EventKind.MEMORY_REVOKED)
        assert payload["memoryId"] == str(memory_id)
        assert payload["by"] == "alice"
        assert payload["reason"] == "it was wrong about the build"
        assert EventKind.APPROVAL_ASKED in kinds(session)
    finally:
        await close(fibers)


async def test_a_forget_nobody_answered_changes_nothing() -> None:
    """The shipped profile has no answerer at all, so this is the default path.

    An ``ask`` that nobody answers is ``UNAVAILABLE``, and every consumer treats
    anything but ``ALLOWED_ONCE`` as a refusal — including this one.
    """
    root, fibers = await build()
    try:
        session = await make_session(root)
        memory_id = await admit(root)

        outcome = await forget(
            session,
            memory=root.memory,
            memory_id=memory_id,
            policy=root.policy,
            approval=root.approval,
            principal=PRINCIPAL,
        )

        assert not outcome.allowed
        assert "unavailable" in outcome.reason, (
            "the refusal says what happened: nobody was there to answer"
        )
        assert not outcome.asked, "'asked' means a person saw the question"
        assert len(await recalled(root)) == 1, "the memory is untouched"
        record = {record.id: record for record in root.memory.records}[memory_id]
        assert record.status is MemoryStatus.ACTIVE
        assert EventKind.MEMORY_REVOKED not in kinds(session), (
            "a refused removal is not written down as a removal"
        )
    finally:
        await close(fibers)


async def test_a_policy_that_denies_forgetting_is_never_asked() -> None:
    """Deny is not "ask harder": the store must not be touched, and no prompt shown."""
    root, fibers = await build({"default": "deny"})
    try:
        session = await make_session(root)
        memory_id = await admit(root)
        root.approval.set_answerer(answer_with(ApprovalOutcome.ALLOWED_ONCE))

        outcome = await forget(
            session,
            memory=root.memory,
            memory_id=memory_id,
            policy=root.policy,
            approval=root.approval,
            principal=PRINCIPAL,
        )

        assert not outcome.allowed
        assert "denies 'forget_memory'" in outcome.reason
        assert EventKind.APPROVAL_ASKED not in kinds(session), "denied before anybody was asked"
        assert len(await recalled(root)) == 1
    finally:
        await close(fibers)


async def test_forgetting_something_that_is_not_there_is_refused_not_ignored() -> None:
    root, fibers = await build()
    try:
        session = await make_session(root)
        root.approval.set_answerer(answer_with(ApprovalOutcome.ALLOWED_ONCE))

        outcome = await forget(
            session,
            memory=root.memory,
            memory_id=MemoryId("mem-nope"),
            policy=root.policy,
            approval=root.approval,
            principal=PRINCIPAL,
        )

        assert not outcome.allowed
        assert "mem-nope" in outcome.reason
        assert EventKind.MEMORY_REVOKED not in kinds(session)
    finally:
        await close(fibers)


# ---------------------------------------------------------------------------
# purging — irreversible, and the only thing that is
# ---------------------------------------------------------------------------


async def test_purging_deletes_the_text_and_leaves_a_tombstone_in_the_log() -> None:
    root, fibers = await build()
    try:
        session = await make_session(root)
        memory_id = await admit(root, text="the customer's dev token lives in the vault")
        root.approval.set_answerer(answer_with(ApprovalOutcome.ALLOWED_ONCE))

        outcome = await purge(
            session,
            memory=root.memory,
            memory_id=memory_id,
            policy=root.policy,
            approval=root.approval,
            principal=PRINCIPAL,
            reason="it named a customer",
        )

        assert outcome.allowed and outcome.tombstone is not None
        assert not await recalled(root)
        assert memory_id not in {record.id for record in root.memory.records}, (
            "purge deletes the content; a status change would not be a deletion"
        )
        payload = data_for(session, EventKind.MEMORY_PURGED)
        assert payload["memoryId"] == str(memory_id)
        assert payload["approvedBy"] == "alice"
        assert payload["reason"] == "it named a customer"
        assert payload["subject"] == "widget build order"
        assert payload["contentHash"], "the tombstone can still match a re-distilled claim"
        assert "token lives in the vault" not in json.dumps(
            [event.to_json() for event in session.events]
        ), "the deleted content is not in the audit either"
    finally:
        await close(fibers)


async def test_a_purge_without_an_approval_deletes_nothing() -> None:
    root, fibers = await build()
    try:
        session = await make_session(root)
        memory_id = await admit(root, text="the customer's dev token lives in the vault")
        root.approval.set_answerer(answer_with(ApprovalOutcome.REJECTED))

        outcome = await purge(
            session,
            memory=root.memory,
            memory_id=memory_id,
            policy=root.policy,
            approval=root.approval,
            principal=PRINCIPAL,
            reason="it named a customer",
        )

        assert not outcome.allowed and outcome.tombstone is None
        assert len(await recalled(root)) == 1
        assert EventKind.MEMORY_PURGED not in kinds(session)
    finally:
        await close(fibers)


async def test_the_tombstone_is_in_the_store_and_the_event() -> None:
    """Two records of the same fact, because the log and the store can disagree.

    The log is append-only and the store is not; if they ever disagree about a
    deletion, the log is the one that was not supposed to be editable.
    """
    root, fibers = await build()
    try:
        session = await make_session(root)
        memory_id = await admit(root)
        before = {record.id: record for record in root.memory.records}[memory_id]
        root.approval.set_answerer(answer_with(ApprovalOutcome.ALLOWED_ONCE))

        outcome = await purge(
            session,
            memory=root.memory,
            memory_id=memory_id,
            policy=root.policy,
            approval=root.approval,
            principal=PRINCIPAL,
            reason="stale",
        )

        assert outcome.tombstone is not None
        in_store = root.memory.tombstones[-1]
        assert in_store == outcome.tombstone
        assert in_store.content_hash == before.content_hash
    finally:
        await close(fibers)


# ---------------------------------------------------------------------------
# resolutions — the bookkeeping, written where the memory was written
# ---------------------------------------------------------------------------


async def test_a_resolution_is_written_once_per_decision() -> None:
    root, fibers = await build()
    try:
        session = await make_session(root)
        decisions = (
            Resolution(
                kind=ResolutionKind.SUPERSEDES,
                incoming=MemoryId("mem-2"),
                incumbent=MemoryId("mem-1"),
                subject="retry loop cap",
                winner=MemoryId("mem-2"),
                reason="test_verified outranks tool_observed",
            ),
            Resolution(
                kind=ResolutionKind.RELATED,
                incoming=MemoryId("mem-2"),
                incumbent=MemoryId("mem-9"),
                subject="the retry loop cap",
                winner=None,
                reason="the subjects look alike",
            ),
        )

        written = await record_resolutions(session, decisions)

        assert written == ("supersedes", "related")
        events = [event for event in session.events if event.kind == EventKind.MEMORY_RESOLVED]
        assert len(events) == 2, "one event per comparison, not one per admission"
        first = dict(events[0].data)
        assert first["winner"] == "mem-2" and first["loser"] == "mem-1"
        assert first["conflict"] is False
        assert dict(events[1].data)["winner"] is None
        assert events[0].is_model_visible is False, (
            "the model reads memories, not the store's bookkeeping"
        )
    finally:
        await close(fibers)


async def test_a_resolution_changes_what_the_next_turn_recalls() -> None:
    """The end of the chain: two claims, the weaker retired, one line recalled.

    This is the plan's §六 counterfactual at the level a person can see. Remove the
    trust order — make the newer record win — and the recalled line is the unchecked
    one, which is exactly what nothing in this repository is allowed to happen.
    """
    from forgepilot.runtime.recall import Recaller, recall_for_turn, render_recalled

    root, fibers = await build()
    try:
        session = await make_session(root)
        checked = await admit(root, text="the cap is five", kind=ProvenanceKind.TEST_VERIFIED)
        seen = await admit(root, text="the cap is three", kind=ProvenanceKind.TOOL_OBSERVED)

        decisions = await root.memory.reconcile(seen)
        await record_resolutions(session, decisions)

        recaller = Recaller(memory=root.memory, scope_ref=REPO.slug)
        recalled_now = await recall_for_turn(
            session,
            recaller=recaller,
            text="what is the retry cap",
            constraints=constraints(),
            turn=None,
        )

        assert [record.id for record in recalled_now] == [checked]
        body = render_recalled(recalled_now)
        assert "the cap is five" in body
        assert "the cap is three" not in body, "the retired claim is not put in front of a model"
        assert [
            event.data["kind"]
            for event in session.events
            if event.kind == EventKind.MEMORY_RESOLVED
        ] == ["superseded"]
        by_id = {record.id: record for record in root.memory.records}
        assert by_id[seen].status is MemoryStatus.SUPERSEDED
        assert by_id[seen].superseded_by == checked
    finally:
        await close(fibers)


async def test_a_witness_is_not_a_status() -> None:
    """Guard against the tempting shortcut: distillation proposes, the store decides.

    ``Witness`` (Phase C) says what a candidate's evidence is worth; it says nothing
    about whether a record is the current belief. Phase F's decisions are all in the
    store, keyed on records — a witness has no status and cannot be reconciled.
    """
    witness = Witness(kind=ProvenanceKind.TEST_VERIFIED, evidence=(), reason="a check passed")

    assert not hasattr(witness, "status")
    assert not hasattr(witness, "reconcile")
    assert witness.is_recallable, "a witness can say what it is worth; it cannot say what stands"


# ---------------------------------------------------------------------------
# the wiring: a distilled memory that disagrees with one already stored
# ---------------------------------------------------------------------------


RULE_A = "the widget build always runs make check before make test"
RULE_B = "the widget build runs make test before make check"
SESSION_A = SessionId("s-conflict-a")
SESSION_B = SessionId("s-conflict-b")
REPLAY_SPEC = "forgepilot.providers.llm.replay:ReplayLLMPlugin"
WATERMARK_SPEC = "forgepilot.providers.distillation.watermark:JsonlWatermarkPlugin"


async def build_distilling(tmp_path: Any):
    """A root that can actually distil: a log, a model, a store and a watermark."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root

    root = create_root()
    fibers: list[Any] = []

    async def load(spec: str, cfg: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), cfg or {})
        fibers.append(fiber)
        return fiber

    await load(SESSION_STORE_SPEC)
    await load(REPLAY_SPEC, {"model": "replay-1"})
    await load(MEMORY_SPEC)
    await load(WATERMARK_SPEC, {"path": str(tmp_path / "watermarks.jsonl")})
    return root, fibers


async def say_and_distil(root: Any, session_id: SessionId, rule: str) -> Any:
    """One session, one thing the user said, distilled into one memory."""
    from forgepilot.runtime.distillation import distill
    from forgepilot.seams.refs import TurnId

    session = await make_session(root, session_id)
    await session.turn_start(TurnId(1))
    said = await session.user_message(TurnId(1), rule)
    await session.turn_end(TurnId(1), "completed")
    await session.flush()

    root.llm.script(
        json.dumps(
            [
                {
                    "type": "fact",
                    "subject": "widget build order",
                    "text": rule,
                    "evidence_seqs": [said.seq],
                    "model_confidence_hint": 0.9,
                }
            ]
        )
    )
    report = await distill(
        session,
        llm=root.llm,
        memory=root.memory,
        watermarks=root.distillationWatermarks,
        scope_ref=REPO.slug,
        tenant_id=str(TENANT),
        repo=REPO,
        model="replay-1",
    )
    return session, report


async def test_distilling_a_contradiction_resolves_it_and_says_so_in_the_log(tmp_path: Any) -> None:
    """The plan's §六, end to end: the comparison happens where memories are written.

    Resolving at recall time instead would put a store write in front of a model that
    is waiting, and would make a turn's context depend on the order the store happened
    to be read in. Both of these claims are backed by the same kind of evidence — a
    person said so — so the later one stands, and the earlier one is kept as history
    with the reason written beside it.
    """
    root, fibers = await build_distilling(tmp_path)
    try:
        session_a, report_a = await say_and_distil(root, SESSION_A, RULE_A)
        assert report_a.written == 1, report_a.notes
        first = MemoryId(report_a.memories[0])

        session_b, report_b = await say_and_distil(root, SESSION_B, RULE_B)
        assert report_b.written == 1, report_b.notes
        second = MemoryId(report_b.memories[0])

        by_id = {record.id: record for record in root.memory.records}
        assert by_id[second].status is MemoryStatus.ACTIVE
        assert by_id[first].status is MemoryStatus.SUPERSEDED, (
            "the claim that was replaced is history, not a competitor"
        )
        assert by_id[first].superseded_by == second
        assert by_id[second].supersedes == first
        assert by_id[first].candidate.text == RULE_A, "history keeps what was claimed"

        resolved = [
            dict(event.data)
            for event in session_b.events
            if event.kind == EventKind.MEMORY_RESOLVED
        ]
        assert len(resolved) == 1, "one comparison, one decision, one line of audit"
        assert resolved[0]["kind"] == "supersedes"
        assert resolved[0]["winner"] == str(second) and resolved[0]["loser"] == str(first)
        assert resolved[0]["reason"], "a resolution without a reason explains nothing later"

        served = await recalled(root)
        assert [record.id for record in served] == [second], "only the standing claim is recalled"
    finally:
        await close(fibers)
