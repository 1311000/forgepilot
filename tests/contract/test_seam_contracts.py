"""Behavioural contract of every seam, exercised through the container.

A Protocol can state that a method exists; it cannot state what the method must
guarantee. The guarantees that matter here are the ones the architecture leans
on — permission re-checks, mandatory revisions, fail-closed approval, and the
provenance gate on memory — so those are what these tests assert.
"""

from __future__ import annotations

import pytest

from forgepilot.providers.llm.replay import text_turn, tool_call_turn
from forgepilot.providers.memory.memory import UnprovenMemoryWrite
from forgepilot.seams import (
    ApprovalOutcome,
    ApprovalService,
    CodeRef,
    CredentialChannel,
    EventRef,
    ExecutionConstraints,
    LineSpan,
    LLMService,
    MemoryCandidate,
    MemoryScope,
    MemoryStore,
    NetworkAccess,
    Operation,
    Principal,
    PrincipalId,
    Provenance,
    ProvenanceKind,
    RecallQuery,
    RepoRef,
    Retriever,
    Sandbox,
    SandboxExecutionPolicy,
    SandboxMode,
    SearchQuery,
    SessionId,
    SymbolKind,
    SymbolQuery,
    TenantId,
    TicketId,
    TicketPriority,
    TicketSource,
    TicketStatus,
    UnattributableScope,
    UnknownCredential,
    Vcs,
    Workspace,
)
from forgepilot.seams.llm import ChatMessage, ChatRequest, Role
from forgepilot.seams.vcs import PullRequestDraft

ACME = RepoRef(host="git.corp", namespace="team", name="svc-a")
OTHER = RepoRef(host="git.corp", namespace="other", name="secret")
ACME_TENANT = TenantId("acme")
ACME_SESSION = SessionId("s-acme")


def event(seq: int, session: SessionId = ACME_SESSION) -> EventRef:
    """A citation into the durable log, which is the only kind evidence can be.

    Evidence used to be a bare string, so a record could cite `"seq:12"` — a
    promise that something at position 12 said this, which no store could check.
    """
    return EventRef(session_id=session, seq=seq)


def constraints_for(
    *repos: RepoRef,
    operations: tuple[Operation, ...] = (
        Operation.READ_CODE,
        Operation.WRITE_CODE,
        Operation.OPEN_PR,
        Operation.RUN_TESTS,
    ),
) -> ExecutionConstraints:
    principal = Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme"))
    return ExecutionConstraints.for_principal(
        principal,
        allowed_repos=frozenset(repos),
        permitted_operations=frozenset(operations),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


# ---------------------------------------------------------------------------
# llm
# ---------------------------------------------------------------------------
async def test_llm_satisfies_seam_and_drains_stream(container) -> None:
    llm = container.llm
    assert isinstance(llm, LLMService)

    llm.script(text_turn("hello"), tool_call_turn("read_file", '{"path": "a.py"}'))
    request = ChatRequest(
        model="replay-test",
        system=("be brief",),
        messages=(ChatMessage(role=Role.USER, content="hi"),),
    )

    first = await llm.complete(request)
    assert first.message.content == "hello"
    assert first.message.tool_calls == ()

    second = await llm.complete(request)
    assert [c.name for c in second.message.tool_calls] == ["read_file"]
    assert second.message.tool_calls[0].arguments_json == '{"path": "a.py"}'


async def test_llm_records_requests_so_prompt_assembly_is_assertable(container) -> None:
    llm = container.llm
    llm.script("ok")
    request = ChatRequest(
        model="replay-test",
        system=("s1", "s2"),
        messages=(ChatMessage(role=Role.USER, content="q"),),
    )
    await llm.complete(request)

    assert len(llm.requests) == 1
    assert llm.requests[0].system == ("s1", "s2")


async def test_llm_running_out_of_script_fails_loudly(container) -> None:
    """Exhaustion must raise: silently inventing a reply would hide a test bug."""
    llm = container.llm
    request = ChatRequest(model="m", system=(), messages=())

    with pytest.raises(AssertionError, match="ran out of scripted turns"):
        await llm.complete(request)


# ---------------------------------------------------------------------------
# retriever
# ---------------------------------------------------------------------------
async def test_retriever_satisfies_seam_and_finds_fixture_text(container) -> None:
    retriever = container.retriever
    assert isinstance(retriever, Retriever)

    retriever.add_file(ACME, "src/login.py", "def retry_login():\n    return None\n")
    constraints = constraints_for(ACME)

    hits = await retriever.search(
        SearchQuery(pattern="retry_login", repos=(ACME,), ref="main"), constraints
    )

    assert len(hits) == 1
    assert hits[0].location.path == "src/login.py"
    assert hits[0].location.ref == "main", "every hit must name its revision"
    assert hits[0].line == 1


async def test_retriever_refuses_repos_outside_constraints(container) -> None:
    """The provider re-checks authority; it does not trust the caller to have done it.

    This is the assertion that makes "a tool bug must not become a leak" real.
    """
    retriever = container.retriever
    retriever.add_file(OTHER, "src/secret.py", "SECRET_TOKEN = 'x'\n")
    constraints = constraints_for(ACME)  # note: ACME only

    hits = await retriever.search(
        SearchQuery(pattern="SECRET_TOKEN", repos=(OTHER,), ref="main"), constraints
    )

    assert hits == ()
    assert retriever.denied_repo_requests == 1

    with pytest.raises(PermissionError):
        await retriever.open(
            CodeRef(repo=OTHER, path="src/secret.py", ref="main"), LineSpan(1, 1), constraints
        )


async def test_retriever_expands_symbols_and_slices_by_span(container) -> None:
    retriever = container.retriever
    retriever.add_file(ACME, "src/a.py", "one\ntwo\nthree\nfour\n")
    retriever.add_symbol(ACME, "src/a.py", "do_thing", SymbolKind.FUNCTION, LineSpan(2, 3))
    constraints = constraints_for(ACME)

    symbols = await retriever.symbols(
        SymbolQuery(name="do_thing", repos=(ACME,), ref="main"), constraints
    )
    assert [s.kind for s in symbols] == [SymbolKind.FUNCTION]

    slice_ = await retriever.open(
        CodeRef(repo=ACME, path="src/a.py", ref="main"), LineSpan(2, 3), constraints
    )
    assert slice_.text == "two\nthree"
    assert slice_.location.ref == "main"


# ---------------------------------------------------------------------------
# tickets
# ---------------------------------------------------------------------------
async def test_ticket_source_round_trips_and_preserves_raw_payload(container) -> None:
    tickets = container.tickets
    assert isinstance(tickets, TicketSource)

    tickets.add_ticket_simple(
        "T-1", "Login retries forever", "steps...", priority=TicketPriority.HIGH,
        raw={"id": "T-1", "workspace_id": "w", "custom_field_17": "keep me"},
    )

    fetched = await tickets.fetch(TicketId("T-1"))
    assert fetched.title == "Login retries forever"
    assert fetched.priority is TicketPriority.HIGH
    # `raw` is kept verbatim so the post-training path needs no schema migration.
    assert fetched.raw["custom_field_17"] == "keep me"

    await tickets.comment(TicketId("T-1"), "investigating")
    assert len((await tickets.fetch(TicketId("T-1"))).comments) == 1

    await tickets.transition(TicketId("T-1"), TicketStatus.IN_PROGRESS)
    assert (await tickets.fetch(TicketId("T-1"))).status is TicketStatus.IN_PROGRESS


async def test_ticket_source_raises_on_unknown_id(container) -> None:
    with pytest.raises(KeyError):
        await container.tickets.fetch(TicketId("nope"))


# ---------------------------------------------------------------------------
# vcs
# ---------------------------------------------------------------------------
async def test_vcs_refuses_pull_request_without_the_operation(container) -> None:
    vcs = container.vcs
    assert isinstance(vcs, Vcs)

    draft = PullRequestDraft(
        repo=ACME,
        source_branch="fix/T-1",
        target_branch="main",
        title="fix",
        body_md="body",
        ticket_id="T-1",
    )
    read_only = constraints_for(ACME, operations=(Operation.READ_CODE,))

    with pytest.raises(PermissionError, match="may not open_pr"):
        await vcs.open_pull_request(draft, read_only)

    assert vcs.pull_requests == ()
    assert vcs.denied_calls == 1


async def test_vcs_opens_pull_request_with_the_operation(container) -> None:
    """No branch or commit setup: a pull request is opened from a *draft*.

    The setup used to create a branch and a commit first, because ``vcs`` owned
    both. It does not any more — a branch and a commit belong to the working tree
    they are made in — and a host is asked to open a PR from a source branch name,
    not from a local commit it has never seen.
    """
    vcs = container.vcs

    ref = await vcs.open_pull_request(
        PullRequestDraft(
            repo=ACME, source_branch="fix/T-1", target_branch="main",
            title="fix", body_md="body", ticket_id="T-1",
        ),
        constraints_for(ACME),
    )

    assert ref.number == 1
    assert len(vcs.pull_requests) == 1
    assert vcs.pull_requests[0].ticket_id == "T-1", "a PR must stay traceable to its ticket"


async def test_vcs_refuses_to_open_a_pull_request_without_the_operation(container) -> None:
    """The boundary that made the move worth doing: publishing is privileged."""
    vcs = container.vcs
    no_open_pr = constraints_for(ACME, operations=(Operation.READ_CODE,))

    with pytest.raises(PermissionError, match="open_pr"):
        await vcs.open_pull_request(
            PullRequestDraft(
                repo=ACME, source_branch="fix/T-1", target_branch="main",
                title="fix", body_md="body",
            ),
            no_open_pr,
        )

    assert vcs.pull_requests == ()


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------
async def test_workspace_prepares_a_resolved_root_and_disposes_it(container) -> None:
    workspace = container.workspace
    assert isinstance(workspace, Workspace)

    handle = await workspace.prepare(ACME, "main", SessionId("s-1"))

    assert handle.root.is_absolute(), "an unresolved root could widen the sandbox boundary"
    assert handle.root.exists()
    assert handle.base_ref == "main"

    # Preparing twice for one session must not create a second workspace.
    assert (await workspace.prepare(ACME, "main", SessionId("s-1"))).root == handle.root

    root = handle.root
    await workspace.dispose(handle)
    assert not root.exists()


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------
async def test_sandbox_records_and_never_executes(container, tmp_path) -> None:
    """The test provider must not be able to run a command.

    If it could, a test that believes it is exercising a refusal path might
    actually be executing something.
    """
    sandbox = container.sandbox
    assert isinstance(sandbox, Sandbox)
    assert sandbox.probe().value == "none"

    result = await sandbox.run(
        ["rm", "-rf", "/"],
        policy=SandboxExecutionPolicy(
            mode=SandboxMode.READ_ONLY,
            workspace_root=tmp_path,
            session_id=SessionId("s-1"),
        ),
        cwd=tmp_path,
        env={},
        timeout_s=1.0,
    )

    assert [c.argv for c in sandbox.calls] == [("rm", "-rf", "/")]
    assert result.exit_code == 0
    # Recording is not executing. Reporting otherwise would let a caller format
    # a command that never ran as a success.
    assert result.executed is False
    assert result.enforcement.value == "none", (
        "the reported enforcement must reflect reality, not the requested policy"
    )


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------
async def test_memory_refuses_to_admit_a_model_assertion(container) -> None:
    """The doc's rule, as a mechanism rather than a convention."""
    memory = container.memory
    assert isinstance(memory, MemoryStore)

    candidate = MemoryCandidate(
        text="the bug is in the retry loop",
        scope=MemoryScope.REPO,
        scope_ref=ACME.slug,
        tenant_id=ACME_TENANT,
    )

    with pytest.raises(UnprovenMemoryWrite, match="guess to fact"):
        await memory.write(
            candidate,
            Provenance(kind=ProvenanceKind.MODEL_ASSERTED, evidence_refs=(event(12),)),
        )

    # It can be quarantined, but quarantine is not recallable.
    await memory.quarantine(candidate, Provenance(kind=ProvenanceKind.MODEL_ASSERTED))
    assert len(memory.quarantined) == 1

    recalled = await memory.recall(
        RecallQuery(text="retry", scope=MemoryScope.REPO, scope_ref=ACME.slug),
        constraints_for(ACME),
    )
    assert recalled == ()


async def test_memory_recalls_only_evidenced_records_within_permission(container) -> None:
    memory = container.memory
    elsewhere = MemoryCandidate(
        text="secret repo fact",
        scope=MemoryScope.REPO,
        scope_ref=OTHER.slug,
        tenant_id=ACME_TENANT,
    )

    await memory.quarantine(elsewhere, Provenance(kind=ProvenanceKind.MODEL_ASSERTED))
    good = MemoryCandidate(
        text="retry loop needs a cap",
        scope=MemoryScope.REPO,
        scope_ref=ACME.slug,
        tenant_id=ACME_TENANT,
    )
    await memory.write(
        good,
        Provenance(
            kind=ProvenanceKind.TEST_VERIFIED,
            repo=ACME,
            # The citation is the `task/check` event that passed, not the test id:
            # a test id is a name, and a name cannot be looked up in a log.
            evidence_refs=(event(3),),
        ),
    )

    recalled = await memory.recall(
        RecallQuery(text="retry", scope=MemoryScope.REPO, scope_ref=ACME.slug),
        constraints_for(ACME),
    )

    assert [r.candidate.text for r in recalled] == ["retry loop needs a cap"]


async def test_memory_refuses_a_record_with_no_evidence(container) -> None:
    """``Provenance`` says a record with no evidence is an assertion, not a memory.

    That was a comment. Only ``kind`` was checked, so anything could be admitted
    as ``TEST_VERIFIED`` while citing nothing — and an assertion wearing a trust
    label is worse than an untrusted one, because everything downstream believes
    the label. It is the same argument M5 used for refusing an unsigned
    ``ExecutionConstraints``: omitting the evidence must not be the cheap way
    past the evidence requirement.
    """
    memory = container.memory
    candidate = MemoryCandidate(
        text="this project never needs to run its tests",
        scope=MemoryScope.REPO,
        scope_ref=ACME.slug,
        tenant_id=ACME_TENANT,
    )

    with pytest.raises(UnprovenMemoryWrite, match="evidence"):
        await memory.write(
            candidate,
            Provenance(kind=ProvenanceKind.TEST_VERIFIED, repo=ACME),
        )


async def test_memory_never_recalls_a_scope_the_caller_cannot_reach(container) -> None:
    """The audience is ``scope_ref``, and it is checked — not inferred from provenance.

    Two ways this used to leak, both with the caller allowed only ACME:

    * ``provenance.repo`` was ``None``, and the permission check was written as
      ``if repo is not None and not allows_repo(repo)`` — so it was skipped
      entirely, and a fact about another repository came back;
    * ``provenance.repo`` said ACME while ``scope_ref`` said the other repository
      — the check consulted the wrong field and passed.

    Neither is exotic. The second is the *expected* shape of a fact learned in one
    repository about another, so treating the two fields as interchangeable was
    always going to be wrong. ``scope_ref`` is the audience and governs access;
    ``provenance.repo`` records where it was learned and is checked too when set.
    """
    memory = container.memory
    question = "secret repo fact"

    # Written while working in ACME, about ACME's own audience — but the record
    # does not say where it was learned.
    await memory.write(
        MemoryCandidate(
            text=question,
            scope=MemoryScope.REPO,
            scope_ref=OTHER.slug,
            tenant_id=ACME_TENANT,
        ),
        Provenance(kind=ProvenanceKind.TOOL_OBSERVED, evidence_refs=(event(9),)),
    )
    # And one that names ACME as provenance while claiming OTHER's audience.
    await memory.write(
        MemoryCandidate(
            text=question + " (mismatched)",
            scope=MemoryScope.REPO,
            scope_ref=OTHER.slug,
            tenant_id=ACME_TENANT,
        ),
        Provenance(kind=ProvenanceKind.TOOL_OBSERVED, repo=ACME, evidence_refs=(event(10),)),
    )

    recalled = await memory.recall(
        RecallQuery(text="", scope=MemoryScope.REPO, scope_ref=OTHER.slug),
        constraints_for(ACME),
    )

    assert recalled == ()


async def test_memory_refuses_a_scope_whose_audience_is_undefined(container) -> None:
    """A team is not a set of repositories, so the audience cannot be derived.

    ``TEAM``/``PROJECT``/``ORG`` name an audience the design never defined — the
    plan assumed a memory scope "mirrors the repository scope", and that
    assumption collapses the moment the scope is not a repository. Until it is
    answered, these scopes are **refused**, and refused *loudly*: an empty result
    would read as "no memories here" when the truth is "nobody has said who may
    read these", and those two must not look alike.

    A caller with access to a repository they cannot reach gets ``()`` — that is
    a permission outcome. An undefined audience is a design gap, and is a raise.

    **The raise belongs at the write** (M27). It used to happen only at recall, and
    the write above succeeded — which means the store accepted a record it could
    never serve: a path answering "yes" to something nobody can see. Both ends
    refuse now; the write because that is where the caller can act on it, and recall
    too because a row written by an older release may still be on disk.
    """
    memory = container.memory
    with pytest.raises(UnattributableScope, match="audience"):
        await memory.write(
            MemoryCandidate(
                text="platform team owns the deploy pipeline",
                scope=MemoryScope.TEAM,
                scope_ref="team-platform",
                tenant_id=ACME_TENANT,
            ),
            Provenance(kind=ProvenanceKind.HUMAN_VERIFIED, evidence_refs=(event(11),)),
        )
    assert not memory.records, "a refused write leaves nothing behind"

    with pytest.raises(UnattributableScope, match="audience"):
        await memory.recall(
            RecallQuery(text="", scope=MemoryScope.TEAM, scope_ref="team-platform"),
            constraints_for(ACME),
        )


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------
async def test_approval_fails_closed_when_there_is_no_answer(container) -> None:
    approval = container.approval
    assert isinstance(approval, ApprovalService)

    from forgepilot.seams.approval import ApprovalRequest

    request = ApprovalRequest(
        request_id="a-1", session_id=SessionId("s-1"), tool_name="shell",
        arguments_json='{"cmd": "rm -rf /"}',
    )

    assert await approval.request(request) == ApprovalOutcome.UNAVAILABLE
    assert approval.requests, "the request must still be visible to a UI"


async def test_approval_is_one_shot(container) -> None:
    from forgepilot.seams.approval import ApprovalRequest

    approval = container.approval
    approval.script(ApprovalOutcome.ALLOWED_ONCE)

    request = ApprovalRequest(
        request_id="a-1", session_id=SessionId("s-1"), tool_name="shell",
        arguments_json="{}",
    )

    assert await approval.request(request) == ApprovalOutcome.ALLOWED_ONCE
    # The grant is consumed: there is no "always allow".
    assert await approval.request(request) == ApprovalOutcome.UNAVAILABLE


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------
async def test_credentials_resolve_a_declared_name_at_resolve_time(container, monkeypatch) -> None:
    """Two guarantees, and the second is the one that is easy to get wrong.

    A channel serves names, not values, and it reads the value **when asked** —
    a deployment loads `.env.local` *after* the container is up, so a snapshot
    taken at boot would resolve to the empty string it saw first and every
    command would run with an empty secret.
    """
    channel = container.credentials
    assert isinstance(channel, CredentialChannel)

    monkeypatch.setenv("FORGEPILOT_TEST_TOKEN", "s3cret-value")
    assert [ref.name for ref in await channel.refs()] == ["demo_token"]
    assert await channel.resolve("demo_token") == "s3cret-value"

    monkeypatch.setenv("FORGEPILOT_TEST_TOKEN", "rotated")
    assert await channel.resolve("demo_token") == "rotated"


async def test_credentials_refuse_a_name_nobody_declared(container) -> None:
    """Refused, and the message lists what *is* declared.

    An empty string here would be worse than an error: it would run the command
    with no credential and fail somewhere else, looking like something else.
    """
    with pytest.raises(UnknownCredential, match="no secret is declared"):
        await container.credentials.resolve("nope")


async def test_credentials_refuse_a_declared_name_that_is_empty(container, monkeypatch) -> None:
    monkeypatch.delenv("FORGEPILOT_TEST_TOKEN", raising=False)

    with pytest.raises(UnknownCredential, match="empty"):
        await container.credentials.resolve("demo_token")
