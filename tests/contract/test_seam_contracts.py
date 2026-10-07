"""Behavioural contract of every seam, exercised through the container.

A Protocol can state that a method exists; it cannot state what the method must
guarantee. The guarantees that matter here are the ones the architecture leans
on — permission re-checks, mandatory revisions, fail-closed approval, and the
provenance gate on memory — so those are what these tests assert.
"""

from __future__ import annotations

import pytest

from forgepilot.seams import (
    ApprovalOutcome,
    ApprovalService,
    CodeRef,
    EventKind,
    ExecutionConstraints,
    LLMService,
    LineSpan,
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
    SessionEvent,
    SessionId,
    SessionStore,
    SymbolKind,
    SymbolQuery,
    TenantId,
    TicketId,
    TicketPriority,
    TicketSource,
    TicketStatus,
    Vcs,
    Workspace,
)
from forgepilot.providers.llm.replay import text_turn, tool_call_turn
from forgepilot.providers.memory.memory import UnprovenMemoryWrite
from forgepilot.seams.llm import ChatMessage, ChatRequest, Role
from forgepilot.seams.vcs import CommitAuthor, PullRequestDraft

ACME = RepoRef(host="git.corp", namespace="team", name="svc-a")
OTHER = RepoRef(host="git.corp", namespace="other", name="secret")


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
    vcs = container.vcs
    await vcs.create_branch(ACME, "fix/T-1", "main", constraints_for(ACME))
    await vcs.commit(
        ACME, "fix/T-1", "fix: retry loop",
        CommitAuthor(name="nil", email="nil@example.com"), constraints_for(ACME),
    )

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
    assert len(vcs.commits) == 1


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

    candidate = MemoryCandidate(text="the bug is in the retry loop", scope=MemoryScope.REPO, scope_ref=ACME.slug)

    with pytest.raises(UnprovenMemoryWrite, match="guess to fact"):
        await memory.write(
            candidate,
            Provenance(kind=ProvenanceKind.MODEL_ASSERTED, evidence_refs=("seq:12",)),
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
    elsewhere = MemoryCandidate(text="secret repo fact", scope=MemoryScope.REPO, scope_ref=OTHER.slug)

    await memory.quarantine(elsewhere, Provenance(kind=ProvenanceKind.MODEL_ASSERTED))
    good = MemoryCandidate(text="retry loop needs a cap", scope=MemoryScope.REPO, scope_ref=ACME.slug)
    await memory.write(
        good,
        Provenance(
            kind=ProvenanceKind.TEST_VERIFIED,
            repo=ACME,
            evidence_refs=("test:tests/test_retry.py::test_cap",),
        ),
    )

    recalled = await memory.recall(
        RecallQuery(text="retry", scope=MemoryScope.REPO, scope_ref=ACME.slug),
        constraints_for(ACME),
    )

    assert [r.candidate.text for r in recalled] == ["retry loop needs a cap"]


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
