"""`read_code`: the consumer `Retriever.open` never had.

`open` was made correct at M14 — it reads the copy the search saw — and had no caller
anywhere in `src/`. A method that only tests call is a claim with no reader, and the
symptom is quiet: the model can *find* code it cannot *read*, so a search result is a
dead end whenever the file is not in the checkout it is standing in. That is the normal
case under the target deployment, where the checkout holds a branch diff and the corpus
lives in an index.

Three behaviours are pinned here, and each is a decision rather than an implementation
detail:

- **The header names the revision that was served.** A retriever may answer from a
  revision the caller is not on; a slice that does not say so is the wrong version of a
  file delivered confidently. The requested ref is a preference, not a filter — the same
  rule `seams/retriever.py::ref_to_serve` documents for search hits.
- **The repository is chosen from the permitted set**, and an unknown one gets the same
  sentence as a forbidden one: "is it there?" is not a question this tool answers.
- **A slice can come from outside the checkout.** `read_code` is not `read_file`: it
  addresses a repository and a revision, and the content may never have been checked out.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgepilot.seams.refs import (
    PrincipalId,
    RepoRef,
    SessionId,
    TenantId,
    TurnId,
)
from forgepilot.seams.retriever import WORKING_TREE
from forgepilot.seams.security import (
    ExecutionConstraints,
    NetworkAccess,
    Operation,
    Principal,
)
from forgepilot.seams.session import SessionHeader
from forgepilot.seams.tools import ToolCall

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")

LOGIN = (
    "MAX_ATTEMPTS = 3\n"
    "\n"
    "\n"
    "def retry_login(client, credentials):\n"
    "    for _ in range(MAX_ATTEMPTS):\n"
    "        return client.login(credentials)\n"
)


def constraints() -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset({REPO}),
        permitted_operations=frozenset({Operation.READ_CODE}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


@pytest.fixture
def checkout(rig) -> Path:
    target = rig.workspace / "src" / "login.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(LOGIN, encoding="utf-8")
    return rig.workspace


async def read(rig, **arguments):
    return await rig.pipeline.invoke(
        ToolCall(call_id="c1", name="read_code", arguments_json=json.dumps(arguments)),
        session=rig.session,
        turn=TurnId(1),
        step=1,
        constraints=constraints(),
    )


# ---------------------------------------------------------------------------
# It exists, and it is the tool `open` was missing
# ---------------------------------------------------------------------------
async def test_the_tool_is_offered_to_the_model(rig) -> None:
    names = {schema.name for schema in rig.registry.schemas()}

    assert "read_code" in names


async def test_it_reads_a_span_from_the_working_tree(rig, checkout) -> None:
    outcome = await read(rig, path="src/login.py", start=1, end=1)

    assert not outcome.is_error, outcome.content
    assert "MAX_ATTEMPTS = 3" in outcome.content
    assert "retry_login" not in outcome.content, "the requested span was not honoured"


async def test_the_header_names_the_revision_that_was_served(rig, checkout) -> None:
    outcome = await read(rig, path="src/login.py", start=1, end=1)

    assert not outcome.is_error, outcome.content
    first = outcome.content.splitlines()[0]
    assert "src/login.py" in first
    assert f"@ {outcome.meta['ref']}" in outcome.content, (
        "a slice that does not say which revision it came from is the wrong version "
        "of a file, delivered confidently"
    )


async def test_the_default_span_is_a_screenful_not_a_file(rig, checkout) -> None:
    outcome = await read(rig, path="src/login.py")

    assert not outcome.is_error, outcome.content
    assert outcome.meta["start"] == 1
    # Short file, so the default span is clamped to what exists rather than invented —
    # and everything from line 1 is what came back.
    assert outcome.meta["end"] == len(LOGIN.splitlines())
    assert "retry_login" in outcome.content


# ---------------------------------------------------------------------------
# Permission, checked here as well as in the seam
# ---------------------------------------------------------------------------
async def test_a_repository_outside_the_grant_is_refused(rig, checkout) -> None:
    outcome = await read(rig, path="src/login.py", repo="git.corp/team/elsewhere")

    assert outcome.is_error
    assert "may read" in outcome.content
    # Equal treatment for "does not exist": the tool must not answer a probe for which
    # repositories this deployment has.
    assert "checkout" not in outcome.content
    assert "configured" not in outcome.content


async def test_an_unknown_path_is_an_error_that_names_it(rig, checkout) -> None:
    outcome = await read(rig, path="src/nowhere.py")

    assert outcome.is_error
    assert "src/nowhere.py" in outcome.content


async def test_an_impossible_span_is_refused_before_anything_is_read(rig, checkout) -> None:
    outcome = await read(rig, path="src/login.py", start=5, end=2)

    assert outcome.is_error
    assert "span" in outcome.content


# ---------------------------------------------------------------------------
# The point: a slice from an index that was never checked out
# ---------------------------------------------------------------------------
class _Indexed:
    """A booted rig whose retriever serves an index and nothing else."""

    def __init__(self, root, pipeline, session, workspace: Path) -> None:
        self.root = root
        self.pipeline = pipeline
        self.session = session
        self.workspace = workspace


@pytest.fixture
async def indexed(tmp_path: Path):
    from forgepilot.kernel import create_root, dispose_fiber
    from forgepilot.providers.retrieve.index import CodeIndexRetrieverPlugin
    from forgepilot.providers.sandbox.recording import RecordingSandboxPlugin
    from forgepilot.providers.session_store.memory import InMemorySessionStorePlugin
    from forgepilot.runtime.approval import ApprovalServicePlugin
    from forgepilot.runtime.guards import GuardRegistry, UsageTracker, default_guards
    from forgepilot.runtime.session import Session
    from forgepilot.runtime.tools.builtin import (
        BuiltinToolsPlugin,
        CodeSearchToolPlugin,
    )
    from forgepilot.runtime.tools.pipeline import ToolPipeline
    from forgepilot.runtime.tools.registry import ToolRegistryPlugin

    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "repo": REPO.slug,
                        "path": "policy/rules/runs.py",
                        "ref": "main",
                        "text": "RUN_LENGTH = 3\nRUNS = 4\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    root = create_root()
    fibers = []

    async def load(plugin, config: dict | None = None):
        fiber = await root.plugin(plugin, config or {})
        fibers.append(fiber)
        return fiber

    await load(InMemorySessionStorePlugin)
    await load(RecordingSandboxPlugin, {"enforcement": "full"})
    await load(ToolRegistryPlugin)
    await load(ApprovalServicePlugin, {"policy": "ask"})
    await load(BuiltinToolsPlugin)
    await load(CodeIndexRetrieverPlugin, {"index_path": str(index)})
    await load(CodeSearchToolPlugin)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    guards = GuardRegistry()
    tracker = UsageTracker()
    for name, guard in default_guards(tracker):
        guards.register(name, guard)
    pipeline = ToolPipeline(
        registry=root.tools,
        guards=guards,
        approval=root.approval,
        enforcement=root.sandbox.probe,
        principal=Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        workspace_root=workspace,
    )
    root.tools.attach_pipeline(pipeline)

    session_id = SessionId("s-indexed")
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=session_id,
            created_at=datetime.now(UTC),
            tenant="acme",
            principal="alice",
        )
    )
    session = await Session.open(root.sessionPersistence, session_id)

    try:
        yield _Indexed(root, pipeline, session, workspace)
    finally:
        for fiber in reversed(fibers):
            await dispose_fiber(fiber)


async def test_a_slice_can_come_from_a_revision_that_was_never_checked_out(indexed) -> None:
    """The reason the tool exists: the corpus is an index, and the checkout has no copy."""
    outcome = await indexed.pipeline.invoke(
        ToolCall(
            call_id="c1",
            name="read_code",
            arguments_json=json.dumps({"path": "policy/rules/runs.py"}),
        ),
        session=indexed.session,
        turn=TurnId(1),
        step=1,
        constraints=constraints(),
    )

    assert not outcome.is_error, outcome.content
    assert "RUN_LENGTH = 3" in outcome.content
    assert not (indexed.workspace / "policy").exists(), "the file was never on disk"
    # Asked for the working tree, served `main`, and the answer says so rather than
    # pretending the two are the same revision.
    assert outcome.meta["askedFor"] == WORKING_TREE
    assert outcome.meta["ref"] == "main"
    assert "asked for" in outcome.content
