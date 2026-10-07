"""A wired task-mode rig: ticket in, branch and pull request out.

Everything runs against mocks and a **scripted** model, so the whole flow is
deterministic and needs neither a network nor a git binary. The sandbox is the
*scripted* one, which stands in for a sandbox that really executed — the recording
one would refuse every command, and a repair loop cannot be exercised against a
sandbox that never returns output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from forgepilot.config import resolve_plugin
from forgepilot.config.project import ProjectConfig
from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.runtime.guards import GuardRegistry, UsageTracker, default_guards
from forgepilot.runtime.session import Session
from forgepilot.runtime.tools.pipeline import ToolPipeline
from forgepilot.seams.refs import PrincipalId, RepoRef, SessionId, TenantId
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal
from forgepilot.seams.session import SessionHeader

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")

#: What a checkout of the project would look like before the agent touches it.
SEED = {
    "src/login.py": "MAX_ATTEMPTS = 3\n\n\ndef retry_login(client, credentials):\n    attempts = 0\n    while True:\n        try:\n            return client.login(credentials)\n        except AuthError:\n            attempts += 1\n",
    "tests/test_login.py": "def test_retry_is_bounded():\n    assert False, 'retry loop is unbounded'\n",
}


@dataclass
class TaskRig:
    root: object
    session: Session
    llm: object
    registry: object
    pipeline: ToolPipeline
    sandbox: object
    vcs: object
    workspace: object
    tracker: UsageTracker
    constraints: ExecutionConstraints
    project: ProjectConfig

    async def close(self) -> None:
        await self.session.close()


def constraints_for(*operations: Operation, writable=("src",)) -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        # The task flow reaches the repository through the VCS seam, which
        # re-checks the repo allow-list itself — so an unset list is a refusal,
        # not an unrestricted default.
        allowed_repos=frozenset({REPO}),
        permitted_operations=frozenset(operations),
        writable_paths=writable,
        network=NetworkAccess.DENY,
    )


PROJECT = ProjectConfig(
    name="svc-a",
    repos=(REPO,),
    repo_prefixes=("git.corp/team/*",),
    default_branch="main",
    test=("pytest -q",),
    allowed_operations=frozenset(Operation),
    writable_paths=("src",),
    tool_sets={
        "default": ("read_file",),
        "bugfix": ("read_file", "write_file", "run"),
    },
    category_tool_sets={"bug": "bugfix"},
)


@pytest.fixture
async def task_rig(tmp_path: Path):
    root = create_root()
    fibers = []

    async def load(spec: str, config: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), config or {})
        fibers.append(fiber)
        return fiber

    await load("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin")
    await load(
        "forgepilot.providers.sandbox.scripted:ScriptedSandboxPlugin",
        {"enforcement": "full"},
    )
    await load("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-1"})
    await load("forgepilot.runtime.tools.registry:ToolRegistryPlugin")
    await load("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "ask"})
    await load("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin")
    await load("forgepilot.providers.vcs.mock:MockVcsPlugin", {})
    await load(
        "forgepilot.providers.workspace.local:LocalTempWorkspacePlugin", {"seed": SEED}
    )

    tracker = UsageTracker()
    guards = GuardRegistry()
    for name, guard in default_guards(tracker):
        guards.register(name, guard)

    pipeline = ToolPipeline(
        registry=root.tools,
        guards=guards,
        approval=root.approval,
        enforcement=root.sandbox.probe,
    )
    root.tools.attach_pipeline(pipeline)

    session_id = SessionId("task-session")
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=session_id,
            created_at=datetime.now(timezone.utc),
            tenant="acme",
            principal="alice",
        )
    )

    try:
        yield TaskRig(
            root=root,
            session=await Session.open(root.sessionPersistence, session_id),
            llm=root.llm,
            registry=root.tools,
            pipeline=pipeline,
            sandbox=root.sandbox,
            vcs=root.vcs,
            workspace=root.workspace,
            tracker=tracker,
            constraints=constraints_for(
                Operation.READ_CODE, Operation.WRITE_CODE, Operation.RUN_TESTS, Operation.OPEN_PR
            ),
            project=PROJECT,
        )
    finally:
        for fiber in reversed(fibers):
            await dispose_fiber(fiber)
