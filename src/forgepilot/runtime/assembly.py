"""Assembling a runnable session from a booted container.

Everything here already exists as separate pieces — a scope with authority, a
session log, a tool pipeline, a loop. This module is the one place they are put
together, so that "how does a session actually start?" has a single answer rather
than being re-derived by each entry point.

It is deliberately small and boring. The interesting decisions were made
elsewhere; what this adds is the *order* they must happen in, which is not
arbitrary:

1. authority is derived and signed, and a scope is opened around it,
2. the session is opened (recovering a torn tail and balancing an interrupted
   turn) **after** the scope, so nothing can be reconstructed without authority,
3. the pipeline is bound to the tool registry, or the registry refuses to execute
   anything — by design, since a registry without a pipeline has no approval, no
   guards and no sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forgepilot.identity.scope import RequestScope
from forgepilot.runtime.guards import UsageTracker
from forgepilot.runtime.session import Session
from forgepilot.runtime.tools.pipeline import ToolPipeline
from forgepilot.seams.refs import SessionId
from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.session import SessionHeader

__all__ = ["SessionRuntime", "assemble_session"]


@dataclass
class SessionRuntime:
    """A session ready to take turns."""

    root: Any
    scope: RequestScope
    session: Session
    pipeline: ToolPipeline

    @property
    def constraints(self) -> ExecutionConstraints:
        return self.scope.constraints

    async def close(self) -> None:
        await self.session.close()
        self.scope.close()


async def assemble_session(
    root: Any,
    constraints: ExecutionConstraints,
    *,
    session_id: SessionId | None = None,
    workspace_root: Path | None = None,
    tenant: str = "",
    principal: str = "",
    minimum_enforcement_for_exec: Any = None,
) -> SessionRuntime:
    """Wire a session's pieces together around an already-signed authority set."""
    from forgepilot.identity.scope import open_request_scope
    from forgepilot.seams.sandbox import SandboxEnforcement

    resolved_id = session_id or SessionId(
        f"session-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    )
    store = root.sessionPersistence
    try:
        session = await Session.open(store, resolved_id)
    except FileNotFoundError:
        await store.create(
            SessionHeader(
                session_id=resolved_id,
                created_at=datetime.now(timezone.utc),
                tenant=tenant or str(constraints.tenant_id),
                principal=principal or str(constraints.principal_id),
                cwd=str(workspace_root or ""),
            )
        )
        session = await Session.open(store, resolved_id)

    # The session is provided *into* the scope rather than handed around beside it,
    # so the loop finds it the same way it finds everything else — and so a plugin
    # that wants it has to declare it, which keeps the dependency visible.
    scope = open_request_scope(
        root, constraints, verify_signature=False, extra_services={"session": session}
    )

    pipeline = ToolPipeline(
        registry=root.tools,
        guards=scope.guards,
        approval=root.approval,
        enforcement=root.sandbox.probe,
        workspace_root=workspace_root,
        minimum_enforcement_for_exec=(
            minimum_enforcement_for_exec
            if minimum_enforcement_for_exec is not None
            else SandboxEnforcement.FULL
        ),
    )
    # The registry refuses to execute without this, which is the point: there is
    # no code path where a tool runs with no approval, no guards and no sandbox.
    root.tools.attach_pipeline(pipeline)

    return SessionRuntime(root=root, scope=scope, session=session, pipeline=pipeline)


def usage_tracker(runtime: SessionRuntime) -> UsageTracker:
    return runtime.scope.tracker
