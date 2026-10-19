"""The memory record's fields — and the reader each one has.

M11a built three mechanics (the provenance gate, the evidence rule, the audience
check) and M11b's Phase B adds the record around them. A field that nothing reads
is the defect shape this repository keeps meeting, so every test here is written
from the reader's side: which decision changes because the field is there.

The reader named in each case:

* ``type``          — where the memory may be recalled (:data:`MEMORY_TYPE_MODES`)
* ``confidence``    — whether it is stated as fact (:attr:`MemoryRecord.is_verified`)
* ``status``        — whether it is served at all, and as what
* ``subject``       — conflict detection, and de-duplication
* ``content_hash``  — one record per claim, however often it is distilled
* ``tenant_id``     — the outermost audience, before the repository one
* ``recall_count``  — "was this ever used", which nothing else can answer
* ``expires_at``    — a plan stops being current; a convention does not

Every case below runs against **every provider of the seam**, not just the in-memory
one: a store that keeps these rules only while the process lives is a cache, and
"which provider am I talking to" must not change the answer to "may this be
believed". The ``container`` fixture at the bottom is what redirects the file; the
parametrisation lives in ``tests/contract/conftest.py::memory_store``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from forgepilot.providers.memory.memory import UnprovenMemoryWrite
from forgepilot.seams import (
    COMPETING_STATUSES,
    CONFIDENCE_FOR_PROVENANCE,
    MEMORY_TYPE_EXPIRY_DAYS,
    MEMORY_TYPE_MODES,
    SUBJECT_SIMILARITY_THRESHOLD,
    TRUST_ORDER,
    Confidence,
    EventRef,
    ExecutionConstraints,
    IllegalTransition,
    MemoryCandidate,
    MemoryId,
    MemoryScope,
    MemoryStatus,
    MemoryStore,
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
    ResolutionKind,
    SessionId,
    TenantId,
    UnattributableScope,
    UnattributableTenant,
    assert_transition,
    confidence_for,
    conflict_key,
    content_hash_of,
    expiry_for,
    normalize_subject,
    resolve_against,
    siblings_of,
    subjects_are_related,
    trust_rank,
)

#: The record factories are the seam's rules applied to a candidate; the stores call
#: them, and the tie test below needs two records stamped with one clock.
from forgepilot.seams.memory import new_recallable_record

ACME = RepoRef(host="git.corp", namespace="team", name="svc-a")
ACME_TENANT = TenantId("acme")
OTHER_TENANT = TenantId("other")
SESSION = SessionId("s-memory")


def ev(seq: int, session: SessionId = SESSION) -> EventRef:
    return EventRef(session_id=session, seq=seq)


def constraints(
    tenant: TenantId = ACME_TENANT, *repos: RepoRef
) -> ExecutionConstraints:
    principal = Principal(principal_id=PrincipalId("alice"), tenant_id=tenant)
    return ExecutionConstraints.for_principal(
        principal,
        allowed_repos=frozenset(repos or (ACME,)),
        permitted_operations=frozenset({Operation.READ_CODE, Operation.RUN_TESTS}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


def candidate(**overrides: object) -> MemoryCandidate:
    base: dict[str, object] = {
        "text": "retry loop needs a cap",
        "scope": MemoryScope.REPO,
        "scope_ref": ACME.slug,
        "type": MemoryType.FACT,
        "subject": "retry loop cap",
        "tenant_id": ACME_TENANT,
    }
    base.update(overrides)
    return MemoryCandidate(**base)  # type: ignore[arg-type]


def provenance(kind: ProvenanceKind, *events: EventRef, **overrides: object) -> Provenance:
    return Provenance(kind=kind, evidence_refs=events, **overrides)  # type: ignore[arg-type]


async def recall_all(memory: MemoryStore, **overrides: object) -> tuple[object, ...]:
    query = RecallQuery(text="", scope=MemoryScope.REPO, scope_ref=ACME.slug)
    if overrides:
        query = RecallQuery(
            text=str(overrides.get("text", "")),
            scope=MemoryScope.REPO,
            scope_ref=ACME.slug,
        )
    return tuple(await memory.recall(query, constraints(**overrides.get("constraints", {}))))


@pytest.fixture
def container(memory_store):
    """The seam's store under test — a different provider for each parameter.

    The bodies below ask ``container.memory`` (and ``container.memory.records``), so a
    one-attribute stand-in is all it takes to put the same questions to every provider
    of the seam. Overriding a conftest fixture inside one module is deliberate: the
    other contract suites still run against the single shared container.
    """
    return SimpleNamespace(memory=memory_store)


# ---------------------------------------------------------------------------
# confidence
# ---------------------------------------------------------------------------
async def test_confidence_is_computed_from_the_evidence_not_from_the_model(container) -> None:
    """The model's number is stored, and then not used for anything.

    A field the model fills in and the system believes is the repeated defect shape
    with a new name, so ``model_confidence_hint`` never reaches the decision: the
    belief level is read off the provenance kind and the count of independent
    events, both of which the harness observed.
    """
    memory = container.memory

    one_effect = await memory.write(candidate(text="the suite runs on pytest"), provenance(ProvenanceKind.TOOL_OBSERVED, ev(1)))
    two_effects = await memory.write(
        candidate(text="the suite runs with pytest-xdist", subject="suite runner"),
        provenance(ProvenanceKind.TOOL_OBSERVED, ev(2), ev(3)),
    )
    guessed = await memory.admit_provisional(
        candidate(
            text="the flaky test is a timing bug",
            subject="flaky test cause",
            model_confidence_hint=0.99,
        ),
        provenance(ProvenanceKind.MODEL_ASSERTED),
    )

    by_id = {record.id: record for record in memory.records}
    assert by_id[one_effect].confidence is Confidence.MEDIUM
    assert by_id[two_effects].confidence is Confidence.HIGH, "a second independent event is more belief"
    assert by_id[guessed].confidence is Confidence.LOW, "0.99 self-reported is still a guess"
    assert by_id[guessed].candidate.model_confidence_hint == 0.99, "kept, just not trusted"
    # The same event cited twice is one event, not two.
    assert confidence_for(ProvenanceKind.TOOL_OBSERVED, (ev(2), ev(2))) is Confidence.MEDIUM


async def test_a_single_event_never_produces_high_for_a_tool_observation(container) -> None:
    assert CONFIDENCE_FOR_PROVENANCE[ProvenanceKind.TOOL_OBSERVED] is Confidence.MEDIUM
    assert CONFIDENCE_FOR_PROVENANCE[ProvenanceKind.MODEL_ASSERTED] is Confidence.LOW
    assert Confidence.HIGH.raised() is Confidence.HIGH, "belief saturates rather than growing"
    assert Confidence.LOW.lowered() is Confidence.LOW


async def test_only_human_or_test_evidence_is_stated_as_fact(container) -> None:
    """``is_verified`` is what a renderer must read before asserting anything."""
    memory = container.memory
    seen = await memory.write(candidate(text="build needs Node 22"), provenance(ProvenanceKind.TOOL_OBSERVED, ev(4)))
    proven = await memory.write(
        candidate(text="the gate is tools/check.py", subject="the gate"),
        provenance(ProvenanceKind.TEST_VERIFIED, ev(5)),
    )
    proposed = await memory.admit_provisional(
        candidate(text="the retry cap should be three", subject="retry cap"),
        provenance(ProvenanceKind.MODEL_ASSERTED),
    )

    by_id = {record.id: record for record in memory.records}
    assert by_id[proven].is_verified is True
    assert by_id[seen].is_verified is False, "observed, not verified — medium, and not asserted"
    assert by_id[proposed].is_verified is False
    assert by_id[proposed].status is MemoryStatus.PROPOSED


# ---------------------------------------------------------------------------
# tenant
# ---------------------------------------------------------------------------
async def test_an_untenanted_candidate_is_refused_rather_than_defaulted(container) -> None:
    """A record nobody can attribute is a record every tenant can read.

    The refusal is the *absence* of a comparison, not a failed one, which is why it
    cannot be a default: defaulting to the caller's tenant would make the leak
    invisible, and defaulting to a shared one would make it total.
    """
    memory = container.memory

    with pytest.raises(UnattributableTenant, match="tenant"):
        await memory.write(
            candidate(tenant_id=None),
            provenance(ProvenanceKind.TEST_VERIFIED, ev(6)),
        )
    with pytest.raises(UnattributableTenant, match="tenant"):
        await memory.admit_provisional(
            candidate(tenant_id=None),
            provenance(ProvenanceKind.MODEL_ASSERTED),
        )
    assert memory.records == ()


async def test_the_tenant_is_checked_before_the_repository_is(container) -> None:
    """Two callers with the same repository access, one tenant apart."""
    memory = container.memory
    await memory.write(
        candidate(text="acme deploys on fridays"),
        provenance(ProvenanceKind.HUMAN_VERIFIED, ev(7)),
    )

    mine = await memory.recall(
        RecallQuery(text="", scope=MemoryScope.REPO, scope_ref=ACME.slug),
        constraints(ACME_TENANT),
    )
    theirs = await memory.recall(
        RecallQuery(text="", scope=MemoryScope.REPO, scope_ref=ACME.slug),
        constraints(OTHER_TENANT),
    )

    assert [record.text for record in mine] == ["acme deploys on fridays"]
    assert theirs == (), "a tenant that is not the record's sees nothing, whatever the repo says"


# ---------------------------------------------------------------------------
# identity: subject and content hash
# ---------------------------------------------------------------------------
async def test_the_same_claim_written_twice_is_one_record(container) -> None:
    """Distillation runs again, and the store does not double.

    The hash deliberately ignores the session, the timestamp and the citing event:
    including any of them would make the key different for the same claim, which is
    the one thing it exists to prevent.
    """
    memory = container.memory
    first = await memory.write(
        candidate(text="the retry loop needs a cap"),
        provenance(ProvenanceKind.TEST_VERIFIED, ev(8)),
    )
    second = await memory.write(
        candidate(text="The Retry  Loop   needs a cap!", subject="Retry Loop Cap"),
        provenance(ProvenanceKind.TEST_VERIFIED, ev(9)),
    )

    assert first == second
    assert len(memory.records) == 1
    assert normalize_subject("Retry  Loop   Cap!") == normalize_subject("retry loop cap")
    # Punctuation and spacing are not a claim, so they are not an identity either:
    # the same sentence with an exclamation mark is the same memory.
    assert content_hash_of(candidate(text="retry loop needs a cap!")) == content_hash_of(
        candidate(text="Retry  Loop needs a cap")
    )


async def test_content_hash_separates_what_it_must_and_ignores_what_it_must_not() -> None:
    base = candidate()
    same_claim_other_session = content_hash_of(candidate())
    assert content_hash_of(base) == same_claim_other_session
    assert content_hash_of(candidate(tenant_id=OTHER_TENANT)) != content_hash_of(base)
    assert content_hash_of(candidate(type=MemoryType.CONVENTION)) != content_hash_of(base)
    assert content_hash_of(candidate(text="something else entirely")) != content_hash_of(base)
    assert content_hash_of(candidate(scope_ref=RepoRef(host="git.corp", namespace="team", name="svc-b").slug)) != content_hash_of(base)


async def test_a_write_over_a_proposal_promotes_it_in_place(container) -> None:
    """A guess that later gets an endorsement must not stay a guess.

    The alternative — returning the existing id and doing nothing — would leave the
    caller believing it had upgraded the record, which is exactly the shape of a
    field that looks enforced and is not.
    """
    memory = container.memory
    proposed = await memory.admit_provisional(
        candidate(text="the cap should be three", subject="retry cap"),
        provenance(ProvenanceKind.MODEL_ASSERTED),
    )
    before = memory.records[0]
    assert before.status is MemoryStatus.PROPOSED

    same = await memory.write(
        candidate(text="the cap should be three", subject="retry cap"),
        provenance(ProvenanceKind.TEST_VERIFIED, ev(10)),
    )
    promoted = memory.records[0]

    assert same == proposed
    assert len(memory.records) == 1
    assert promoted.status is MemoryStatus.ACTIVE
    assert promoted.confidence is Confidence.HIGH
    assert promoted.is_verified is True


async def test_admit_provisional_refuses_to_bury_an_endorsement(container) -> None:
    memory = container.memory
    with pytest.raises(UnprovenMemoryWrite, match="proposal"):
        await memory.admit_provisional(
            candidate(),
            provenance(ProvenanceKind.TEST_VERIFIED, ev(11)),
        )


# ---------------------------------------------------------------------------
# status: confirm, revoke, states
# ---------------------------------------------------------------------------
async def test_confirm_promotes_a_proposal_and_keeps_its_history(container) -> None:
    memory = container.memory
    proposed = await memory.admit_provisional(
        candidate(text="we do not touch the vendor directory", subject="vendor directory"),
        provenance(ProvenanceKind.MODEL_ASSERTED, ev(12)),
    )

    confirmed = await memory.confirm(proposed, ev(13))

    assert confirmed.status is MemoryStatus.ACTIVE
    assert confirmed.confidence is Confidence.HIGH
    assert confirmed.provenance.kind is ProvenanceKind.HUMAN_VERIFIED
    assert [ref.seq for ref in confirmed.provenance.evidence_refs] == [12, 13], (
        "upgrading a record must not shrink the evidence it already had"
    )
    assert memory.records[0].status is MemoryStatus.ACTIVE


async def test_confirm_refuses_a_record_that_cannot_come_back(container) -> None:
    memory = container.memory
    admitted = await memory.write(
        candidate(text="the mirror lives on nexus", subject="mirror"),
        provenance(ProvenanceKind.TOOL_OBSERVED, ev(14)),
    )
    await memory.revoke(admitted)

    with pytest.raises(IllegalTransition, match="revoked"):
        await memory.confirm(admitted, ev(15))
    with pytest.raises(KeyError):
        await memory.confirm("mem-nope", ev(15))


async def test_revoked_and_superseded_records_are_history(container) -> None:
    memory = container.memory
    admitted = await memory.write(
        candidate(text="the nightly job is at 02:00", subject="nightly job"),
        provenance(ProvenanceKind.TOOL_OBSERVED, ev(16)),
    )
    assert len(memory.records) == 1

    await memory.revoke(admitted)

    assert memory.records[0].status is MemoryStatus.REVOKED
    assert await memory.recall(
        RecallQuery(text="", scope=MemoryScope.REPO, scope_ref=ACME.slug), constraints()
    ) == ()
    # Revoking is a status, not a deletion: the record is still there to be audited.
    assert len(memory.records) == 1
    # And the claim may be made again, because nothing live holds that content.
    again = await memory.write(
        candidate(text="the nightly job is at 03:00", subject="nightly job"),
        provenance(ProvenanceKind.TOOL_OBSERVED, ev(17)),
    )
    assert again != admitted


async def test_the_state_machine_names_both_ends_when_it_refuses() -> None:
    assert_transition(MemoryStatus.SUPERSEDED, MemoryStatus.SUPERSEDED)  # idempotent
    assert_transition(MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED)
    assert_transition(MemoryStatus.CONFLICTED, MemoryStatus.ACTIVE)

    for current, target in (
        (MemoryStatus.SUPERSEDED, MemoryStatus.ACTIVE),
        (MemoryStatus.REVOKED, MemoryStatus.ACTIVE),
        (MemoryStatus.EXPIRED, MemoryStatus.PROPOSED),
        (MemoryStatus.PROPOSED, MemoryStatus.CONFLICTED),
    ):
        with pytest.raises(IllegalTransition) as refused:
            assert_transition(current, target)
        assert current.value in str(refused.value) and target.value in str(refused.value)


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------
async def test_a_memory_past_its_deadline_is_not_recalled_and_is_marked_expired(container) -> None:
    """``expires_at`` is observed at recall, not swept by a timer nobody runs.

    A plan is true until it is not; a convention has no deadline at all. The record
    keeps its evidence and its history when it expires — expiry is a status.
    """
    memory = container.memory
    stale = await memory.write(
        candidate(
            text="the current era is memory distillation",
            subject="current era",
            type=MemoryType.PLAN,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
        provenance(ProvenanceKind.HUMAN_VERIFIED, ev(18)),
    )
    fresh = await memory.write(
        candidate(text="only kernel/ imports cordis_port", subject="cordis_port import", type=MemoryType.CONVENTION),
        provenance(ProvenanceKind.TEST_VERIFIED, ev(19)),
    )

    hits = await memory.recall(
        RecallQuery(text="", scope=MemoryScope.REPO, scope_ref=ACME.slug), constraints()
    )
    by_id = {record.id: record for record in memory.records}
    assert [record.id for record in hits] == [fresh]
    assert by_id[stale].status is MemoryStatus.EXPIRED
    assert by_id[stale].provenance.evidence_refs, "the evidence survives the expiry"


# ---------------------------------------------------------------------------
# recall accounting, and the type/mode table
# ---------------------------------------------------------------------------
async def test_recall_counts_only_what_it_returned(container) -> None:
    """A count nobody increments is the defect shape in miniature."""
    memory = container.memory
    admitted = await memory.write(
        candidate(text="retry loop needs a cap", subject="retry cap"),
        provenance(ProvenanceKind.TEST_VERIFIED, ev(20)),
    )

    await memory.recall(
        RecallQuery(text="nothing matches this", scope=MemoryScope.REPO, scope_ref=ACME.slug),
        constraints(),
    )
    by_id = {record.id: record for record in memory.records}
    assert by_id[admitted].recall_count == 0, "a query that matched nothing is not a use"

    hit = await memory.recall(
        RecallQuery(text="cap", scope=MemoryScope.REPO, scope_ref=ACME.slug), constraints()
    )
    assert hit[0].recall_count == 1
    assert hit[0].last_recalled_at is not None


async def test_every_type_names_where_it_may_be_recalled() -> None:
    """The table is the reader for ``type`` — this is what keeps a task summary
    from being recited back during a chat."""
    assert set(MEMORY_TYPE_MODES) == set(MemoryType), "a type with no mode is a type nobody reads"
    assert MEMORY_TYPE_MODES[MemoryType.TASK_SUMMARY] == frozenset({Mode.TASK})
    assert Mode.CHAT not in MEMORY_TYPE_MODES[MemoryType.TASK_SUMMARY]
    assert Mode.REVIEW in MEMORY_TYPE_MODES[MemoryType.CONVENTION]
    assert Mode.REVIEW not in MEMORY_TYPE_MODES[MemoryType.COMMAND]


async def test_the_schema_is_exported_from_the_seam_namespace() -> None:
    """A caller imports one module, not a path."""
    import forgepilot.seams as seams

    for name in (
        "Confidence",
        "EventRef",
        "IllegalTransition",
        "MemoryStatus",
        "MemoryType",
        "UnattributableTenant",
        "assert_transition",
        "confidence_for",
        "content_hash_of",
        "normalize_subject",
    ):
        assert name in seams.__all__, f"{name} is re-exported but unreachable"


# ---------------------------------------------------------------------------
# Phase F — which of two memories stands, when one stops being current, and what
# forgetting costs. The plan's §六.
# ---------------------------------------------------------------------------
#
# The reader of the new fields, named as the module docstring asks:
#
# * ``supersedes`` / ``superseded_by`` — the chain a replacement leaves behind, so a
#   memory that changed has a history rather than a gap
# * ``status = CONFLICTED``          — the harness refusing to pick between two claims
#   it cannot separate, which is a *result*, not a failure to compute one
# * ``expires_at`` (per type)        — a plan stops being current after 90 days; a
#   convention does not stop being true
# * the tombstone                    — the only thing left after ``purge``, and the
#   only reason "who removed this?" is answerable at all


def test_the_trust_order_is_total_and_starts_with_a_person() -> None:
    """The order is the mechanism. Everything else in Phase F is bookkeeping."""
    assert TRUST_ORDER[0] is ProvenanceKind.HUMAN_VERIFIED
    assert TRUST_ORDER[-1] is ProvenanceKind.MODEL_ASSERTED
    assert trust_rank(ProvenanceKind.HUMAN_VERIFIED) > trust_rank(ProvenanceKind.TEST_VERIFIED)
    assert trust_rank(ProvenanceKind.TEST_VERIFIED) > trust_rank(ProvenanceKind.TOOL_OBSERVED)
    assert trust_rank(ProvenanceKind.TOOL_OBSERVED) > trust_rank(ProvenanceKind.MODEL_ASSERTED)
    assert len({trust_rank(kind) for kind in ProvenanceKind}) == len(TRUST_ORDER), (
        "two kinds with one rank would make 'who wins' depend on dictionary order"
    )


def test_a_tie_at_equal_trust_is_a_conflict_and_not_a_coin_toss() -> None:
    """Two claims the harness cannot separate are both kept and both marked.

    Silent last-writer-wins is the failure this exists to prevent: the store would look
    decisive and there would be nothing in the log to show a decision was ever made.
    """
    when = datetime(2026, 1, 1, tzinfo=UTC)
    incumbent = new_recallable_record(
        MemoryId("mem-1"),
        candidate(text="the cap is 3"),
        provenance(ProvenanceKind.TOOL_OBSERVED, ev(1)),
        now=when,
    )
    incoming = new_recallable_record(
        MemoryId("mem-2"),
        candidate(text="the cap is 5"),
        provenance(ProvenanceKind.TOOL_OBSERVED, ev(2)),
        now=when,
    )

    decision = resolve_against(incumbent, incoming, now=when)

    assert decision.kind is ResolutionKind.CONFLICT
    assert decision.winner is None and decision.loser is None
    assert "conflict" in decision.reason


def test_a_conflict_names_both_records_and_a_supersession_names_the_winner() -> None:
    """A resolution that cannot be read back is not an audit trail."""
    when = datetime(2026, 1, 1, tzinfo=UTC)
    checked = new_recallable_record(
        MemoryId("mem-1"),
        candidate(),
        provenance(ProvenanceKind.TEST_VERIFIED, ev(1)),
        now=when,
    )
    seen = new_recallable_record(
        MemoryId("mem-2"),
        candidate(),
        provenance(ProvenanceKind.TOOL_OBSERVED, ev(2)),
        now=when,
    )

    decision = resolve_against(checked, seen, now=when)

    assert decision.kind is ResolutionKind.SUPERSEDED, "the incoming record is the one that loses"
    assert decision.winner == MemoryId("mem-1") and decision.loser == MemoryId("mem-2")
    assert str(checked.provenance.kind) in decision.reason
    assert str(seen.provenance.kind) in decision.reason


def test_a_subject_deadline_is_per_type_unless_the_candidate_names_one() -> None:
    """``expiry_for`` is the reader of ``MEMORY_TYPE_EXPIRY_DAYS``.

    The seam factory — not the caller — applies it, because a per-type default that
    every write path has to remember is one that gets forgotten on the next write path.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)

    plan = expiry_for(candidate(type=MemoryType.PLAN), now=now)
    summary = expiry_for(candidate(type=MemoryType.TASK_SUMMARY), now=now)
    convention = expiry_for(candidate(type=MemoryType.CONVENTION), now=now)

    assert plan == now + timedelta(days=MEMORY_TYPE_EXPIRY_DAYS[MemoryType.PLAN])
    assert summary == now + timedelta(days=90)
    assert convention is None, "a convention does not stop being true"
    explicit = now + timedelta(days=1)
    assert expiry_for(candidate(type=MemoryType.PLAN, expires_at=explicit), now=now) == explicit


async def test_a_plan_written_through_the_store_carries_its_deadline(container) -> None:
    """The type policy is applied on the path everything actually uses."""
    memory = container.memory
    before = datetime.now(UTC)

    plan = await memory.write(
        candidate(type=MemoryType.PLAN, subject="the migration order"),
        provenance(ProvenanceKind.HUMAN_VERIFIED, ev(1)),
    )
    fact = await memory.write(
        candidate(subject="the retry cap"), provenance(ProvenanceKind.HUMAN_VERIFIED, ev(2))
    )

    by_id = {record.id: record for record in memory.records}
    assert by_id[plan].expires_at is not None
    assert by_id[plan].expires_at > before + timedelta(days=89)
    assert by_id[fact].expires_at is None


async def test_a_later_claim_does_not_replace_the_one_that_was_checked(container) -> None:
    """The counterfactual the plan asks for: remove the order and this goes red.

    ``TOOL_OBSERVED`` was written *second* and is therefore newer. By age it wins; by
    anything worth relying on it does not, and the record that a check passed stays the
    one that is served.
    """
    memory = container.memory
    checked = await memory.write(
        candidate(text="the retry loop needs a cap"),
        provenance(ProvenanceKind.TEST_VERIFIED, ev(1)),
    )
    seen = await memory.write(
        candidate(text="the retry loop needs no cap"),
        provenance(ProvenanceKind.TOOL_OBSERVED, ev(2)),
    )

    decisions = await memory.reconcile(seen)

    assert [decision.kind for decision in decisions] == [ResolutionKind.SUPERSEDED]
    assert decisions[0].winner == checked
    by_id = {record.id: record for record in memory.records}
    assert by_id[checked].status is MemoryStatus.ACTIVE
    assert by_id[seen].status is MemoryStatus.SUPERSEDED
    assert by_id[seen].superseded_by == checked
    served = await recall_all(memory)
    assert [record.id for record in served] == [checked], "the loser is not served"


async def test_a_better_claim_written_later_replaces_the_weaker_one_and_says_so(container) -> None:
    """The other direction, and the chain it leaves: the old record is history, not gone.

    The two claims differ in wording on purpose: the identical text would be *one*
    record — that is what the content hash is for — and there would be nothing to
    resolve.
    """
    memory = container.memory
    seen = await memory.write(
        candidate(text="the cap is three"), provenance(ProvenanceKind.TOOL_OBSERVED, ev(1))
    )
    checked = await memory.write(
        candidate(text="the cap is five"), provenance(ProvenanceKind.TEST_VERIFIED, ev(2))
    )

    decisions = await memory.reconcile(checked)

    assert [decision.kind for decision in decisions] == [ResolutionKind.SUPERSEDES]
    by_id = {record.id: record for record in memory.records}
    assert by_id[checked].supersedes == seen, "the replacement names what it replaced"
    assert by_id[seen].status is MemoryStatus.SUPERSEDED
    assert by_id[seen].superseded_by == checked
    assert by_id[seen].candidate.text == "the cap is three", "history keeps the claim"
    served = await recall_all(memory)
    assert [record.id for record in served] == [checked]


async def test_a_proposal_neither_wins_nor_is_retired(container) -> None:
    """A candidate is not a belief: it cannot outrank a claim, and a claim does not
    make it history. Superseding it would hide something a person might still confirm.
    """
    memory = container.memory
    admitted = await memory.write(
        candidate(text="the retry loop needs a cap"),
        provenance(ProvenanceKind.TEST_VERIFIED, ev(1)),
    )
    proposed = await memory.admit_provisional(
        candidate(text="the retry loop needs a cap of five"),
        provenance(ProvenanceKind.MODEL_ASSERTED, ev(2)),
    )

    assert await memory.reconcile(proposed) == (), "a proposal resolves nothing"
    assert await memory.reconcile(admitted) == (), "and is not resolved by anything"

    by_id = {record.id: record for record in memory.records}
    assert by_id[admitted].status is MemoryStatus.ACTIVE
    assert by_id[proposed].status is MemoryStatus.PROPOSED
    assert MemoryStatus.PROPOSED not in COMPETING_STATUSES, (
        "a candidate is not a belief, and this set is what says so"
    )


async def test_two_subjects_that_look_alike_are_reported_and_nothing_is_demoted(container) -> None:
    """词面相似度兜底, with the limit the plan does not state: it tells a person, and
    it does not decide. A fuzzy match is not grounds to demote a verified record."""
    memory = container.memory
    first = await memory.write(
        candidate(subject="retry loop cap"), provenance(ProvenanceKind.TEST_VERIFIED, ev(1))
    )
    second = await memory.write(
        candidate(subject="the retry loop cap"), provenance(ProvenanceKind.TEST_VERIFIED, ev(2))
    )

    decisions = await memory.reconcile(second)

    assert [decision.kind for decision in decisions] == [ResolutionKind.RELATED]
    assert decisions[0].winner is None
    by_id = {record.id: record for record in memory.records}
    assert by_id[first].status is MemoryStatus.ACTIVE
    assert by_id[second].status is MemoryStatus.ACTIVE


def test_relatedness_is_a_threshold_over_the_same_folding_subjects_use() -> None:
    assert subjects_are_related("retry loop cap", "the retry loop cap")
    assert not subjects_are_related("retry loop cap", "the database migration order")
    assert not subjects_are_related("retry loop cap", ""), "an empty subject relates to nothing"
    assert subjects_are_related("Retry Loop Cap", "retry-loop-cap"), "case and punctuation fold"
    assert subjects_are_related(
        "retry loop cap", "the retry loop cap", threshold=SUBJECT_SIMILARITY_THRESHOLD
    )
    assert not subjects_are_related("retry loop cap", "the retry loop cap", threshold=0.9), (
        "the threshold is a knob; the default is the one the report uses"
    )


def test_siblings_are_the_records_under_one_key() -> None:
    when = datetime(2026, 1, 1, tzinfo=UTC)
    here = new_recallable_record(
        MemoryId("mem-1"), candidate(), provenance(ProvenanceKind.TEST_VERIFIED, ev(1)), now=when
    )
    same = new_recallable_record(
        MemoryId("mem-2"), candidate(), provenance(ProvenanceKind.TEST_VERIFIED, ev(2)), now=when
    )
    other_subject = new_recallable_record(
        MemoryId("mem-3"),
        candidate(subject="something else"),
        provenance(ProvenanceKind.TEST_VERIFIED, ev(3)),
        now=when,
    )
    other_tenant = new_recallable_record(
        MemoryId("mem-4"),
        candidate(tenant_id=OTHER_TENANT),
        provenance(ProvenanceKind.TEST_VERIFIED, ev(4)),
        now=when,
    )

    assert conflict_key(here) == conflict_key(same)
    assert {record.id for record in siblings_of((here, same, other_subject, other_tenant), here)} == {
        MemoryId("mem-2")
    }, "a subject means nothing across a tenant, a scope, a type or another subject"


async def test_forgetting_keeps_the_record_and_purging_does_not(container) -> None:
    """The two halves of 遗忘, which is the whole point of having two.

    ``revoke`` is reversible and keeps the evidence; ``purge`` deletes the content and
    leaves only what is needed to answer "who removed this, and when".
    """
    memory = container.memory
    first = await memory.write(candidate(), provenance(ProvenanceKind.TEST_VERIFIED, ev(1)))
    await memory.revoke(first)

    forgotten = {record.id: record for record in memory.records}[first]
    assert forgotten.status is MemoryStatus.REVOKED
    assert forgotten.candidate.text == "retry loop needs a cap", "forgetting is not deleting"
    assert await recall_all(memory) == ()

    second = await memory.write(
        candidate(text="the migration order"), provenance(ProvenanceKind.TEST_VERIFIED, ev(2))
    )
    before = {record.id: record for record in memory.records}[second]
    tombstone = await memory.purge(second, approved_by="alice", reason="it named a customer")

    assert tombstone.memory_id == second
    assert tombstone.approved_by == "alice" and tombstone.reason == "it named a customer"
    assert tombstone.content_hash == before.content_hash, "the tombstone names what was deleted"
    assert tombstone.subject == before.subject
    assert second not in {record.id for record in memory.records}, "the content is gone"
    assert memory.tombstones and memory.tombstones[-1].memory_id == second
    assert await recall_all(memory) == (), "a purged memory is not served, and not a corpse either"


async def test_forgetting_or_purging_something_that_is_not_there_is_refused(container) -> None:
    """A no-op would be reported as a successful removal, which is the worst of both."""
    memory = container.memory
    with pytest.raises(KeyError):
        await memory.revoke(MemoryId("mem-nope"))
    with pytest.raises(KeyError):
        await memory.purge(MemoryId("mem-nope"), approved_by="alice")


# ---------------------------------------------------------------------------
# M27: admission-time audience, and the aging-out path
# ---------------------------------------------------------------------------


async def test_a_team_scoped_candidate_is_refused_at_the_write(memory_store) -> None:
    """A scope with no audience model cannot be admitted at all.

    ``audience_repo`` raised at *recall* only, so a ``TEAM``-scoped candidate used to
    be written and then be unreadable forever — a record nobody can see, created by a
    path that answered yes. The refusal belongs where the caller can still act on it.
    """
    with pytest.raises(UnattributableScope):
        await memory_store.write(
            candidate(scope=MemoryScope.TEAM, scope_ref="team-x"),
            provenance(ProvenanceKind.HUMAN_VERIFIED, ev(1)),
        )


async def test_prune_deletes_history_and_never_a_belief(memory_store) -> None:
    """Aging-out is for rows no recall could have served.

    The active record is the store's current belief, so it is never old enough to
    delete however old it is; the revoked one is terminal history. Deleting the
    belief instead would be a silent data loss dressed as housekeeping.
    """
    from datetime import UTC, datetime, timedelta

    belief = await memory_store.write(
        candidate(text="retry loop needs a cap", subject="keep me"),
        provenance(ProvenanceKind.HUMAN_VERIFIED, ev(1)),
    )
    history = await memory_store.write(
        candidate(text="the build used make", subject="retire me"),
        provenance(ProvenanceKind.HUMAN_VERIFIED, ev(2)),
    )
    await memory_store.revoke(history)

    report = await memory_store.prune(
        now=datetime.now(UTC) + timedelta(days=200), older_than_days=90
    )

    assert history in report.removed
    assert belief not in report.removed
    assert report.count == 1


async def test_prune_reports_without_deleting_when_asked_to(memory_store) -> None:
    """``dry_run`` is how an operator sees the number before it is the number."""
    from datetime import UTC, datetime, timedelta

    history = await memory_store.write(
        candidate(text="the build used make", subject="retire me"),
        provenance(ProvenanceKind.HUMAN_VERIFIED, ev(2)),
    )
    await memory_store.revoke(history)
    later = datetime.now(UTC) + timedelta(days=200)

    preview = await memory_store.prune(now=later, older_than_days=90, dry_run=True)
    assert history in preview.removed
    assert preview.dry_run is True

    real = await memory_store.prune(now=later, older_than_days=90)
    assert real.removed == preview.removed, "the dry run named what the real one removed"

    empty = await memory_store.prune(now=later, older_than_days=90)
    assert empty.removed == (), "it is gone, so a second prune has nothing to name"


async def test_prune_leaves_recent_history_alone(memory_store) -> None:
    """Age is ``updated_at or created_at``: a row superseded yesterday is new news."""
    from datetime import UTC, datetime

    history = await memory_store.write(
        candidate(text="the build used make", subject="retire me"),
        provenance(ProvenanceKind.HUMAN_VERIFIED, ev(2)),
    )
    await memory_store.revoke(history)

    report = await memory_store.prune(now=datetime.now(UTC), older_than_days=90)
    assert report.removed == ()
