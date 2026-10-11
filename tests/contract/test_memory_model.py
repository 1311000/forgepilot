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
    CONFIDENCE_FOR_PROVENANCE,
    MEMORY_TYPE_MODES,
    Confidence,
    EventRef,
    ExecutionConstraints,
    IllegalTransition,
    MemoryCandidate,
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
    SessionId,
    TenantId,
    UnattributableTenant,
    assert_transition,
    confidence_for,
    content_hash_of,
    normalize_subject,
)

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
