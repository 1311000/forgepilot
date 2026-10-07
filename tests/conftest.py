"""A fully wired tool pipeline for the M4 suites.

Everything here goes through the real container: the registry, the sandbox, the
approval service and the shipped tools are all registered as plugins, so the
tests exercise the same wiring production will use rather than a hand-assembled
approximation of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from forgepilot.config import resolve_plugin
from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.runtime.guards import GuardRegistry, UsageTracker, default_guards
from forgepilot.runtime.session import Session
from forgepilot.runtime.tools.pipeline import ToolPipeline
from forgepilot.seams.refs import PrincipalId, SessionId, TenantId
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal
from forgepilot.seams.session import SessionHeader


@dataclass
class Rig:
    """Everything a pipeline test needs to drive and inspect one call."""

    root: object
    pipeline: ToolPipeline
    session: Session
    store: object
    sandbox: object
    approval: object
    registry: object
    tracker: UsageTracker
    guards: GuardRegistry
    workspace: Path


def constraints_for(
    workspace: Path,
    *,
    repos: tuple = (),
    prefixes: frozenset[str] = frozenset(),
    operations: tuple[Operation, ...] = (
        Operation.READ_CODE,
        Operation.WRITE_CODE,
        Operation.RUN_TESTS,
        Operation.OPEN_PR,
    ),
    writable: tuple[str, ...] = ("src",),
    network: NetworkAccess = NetworkAccess.DENY,
) -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset(repos),
        allowed_repo_prefixes=prefixes,
        permitted_operations=frozenset(operations),
        writable_paths=writable,
        network=network,
    )


@pytest.fixture
async def rig(tmp_path: Path):
    """A booted pipeline with one open session."""
    from datetime import datetime, timezone

    root = create_root()
    fibers = []

    async def load(spec: str, config: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), config or {})
        fibers.append(fiber)
        return fiber

    await load("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin")
    # `full` enforcement so that exec-capable tools can be armed; the arm phase's
    # refusal under weaker enforcement has its own test.
    await load(
        "forgepilot.providers.sandbox.recording:RecordingSandboxPlugin",
        {"enforcement": "full"},
    )
    await load("forgepilot.runtime.tools.registry:ToolRegistryPlugin")
    await load("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "ask"})
    await load("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin")

    tracker = UsageTracker()
    guards = GuardRegistry()
    for name, guard in default_guards(tracker):
        guards.register(name, guard)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "src").mkdir()

    pipeline = ToolPipeline(
        registry=root.tools,
        guards=guards,
        approval=root.approval,
        enforcement=root.sandbox.probe,
        workspace_root=workspace,
    )
    root.tools.attach_pipeline(pipeline)

    session_id = SessionId("s-under-test")
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=session_id,
            created_at=datetime.now(timezone.utc),
            tenant="acme",
            principal="alice",
        )
    )
    session = await Session.open(root.sessionPersistence, session_id)

    try:
        yield Rig(
            root=root,
            pipeline=pipeline,
            session=session,
            store=root.sessionPersistence,
            sandbox=root.sandbox,
            approval=root.approval,
            registry=root.tools,
            tracker=tracker,
            guards=guards,
            workspace=workspace,
        )
    finally:
        for fiber in reversed(fibers):
            await dispose_fiber(fiber)
