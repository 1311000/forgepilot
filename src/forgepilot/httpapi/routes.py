"""The HTTP surface — the boundary the frontend can be swapped behind.

Two rules shape every endpoint here, and both are about what the API *is not*:

**It does not decide anything.** Every request re-authenticates and re-derives
authority through ``deps.resolve_credential``; every session runs under the scope
``assemble_session`` opened, with the constraints that came from that principal.
Nothing in this module constructs an authority, widens one, or caches one.

**It does not own the timeline.** Events come from the session log, in log order,
carrying their ``seq``. The API is a reader, so it cannot show a client something
the log does not say — including the ordering that makes an approval prompt
precede the tool body it authorises.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from forgepilot.errors import ForgePilotError
from forgepilot.httpapi.registry import SessionNotFound, SessionRegistry, SessionStopping
from forgepilot.httpapi.sse import LostStream, tail_session
from forgepilot.seams.approval import ApprovalOutcome
from forgepilot.seams.refs import AgentId, Mode, SessionId

__all__ = ["ApiError", "ChatRequest", "Decision", "build_router"]


class ApiError(ForgePilotError):
    """An error with a status attached, so the boundary maps it once."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class ChatRequest:
    text: str


@dataclass(frozen=True, slots=True)
class Decision:
    decision: str


#: The only two words a client may use. Deliberately not the enum's full set: a
#: client has no business saying ``unavailable`` (that is the *absence* of an
#: answer) or ``cancelled`` (that is the harness withdrawing a question).
_ALLOW = "allowed-once"
_DENY = "rejected"


def build_router(
    root: Any,
    registry: SessionRegistry,
    *,
    workspace_root: Path | None = None,
    policy: Any = None,
    project: Any = None,
    project_configured: bool = False,
    metrics: Any = None,
):
    """Build the FastAPI router.

    Imported inside the function so that the ``web`` extra stays genuinely
    optional: the rest of ForgePilot must import and run without a web stack
    installed, and a module-level ``import fastapi`` would break that.
    """
    # Imported here, not at module scope: `web` is an optional extra, and the
    # rest of ForgePilot must import and run without a web stack installed.
    from fastapi import APIRouter, Depends, Header, Query
    from fastapi.responses import JSONResponse
    from sse_starlette.sse import EventSourceResponse

    from forgepilot.httpapi.app import credential_dependency

    router = APIRouter()
    who = credential_dependency(root, policy)

    from forgepilot.httpapi.tasks import TaskRefused, TaskService

    tasks = TaskService(
        root,
        registry,
        project=project,
        workspace_root=workspace_root,
        project_configured=project_configured,
        metrics=metrics,
    )

    # -- sessions ----------------------------------------------------------
    @router.get("/sessions")
    async def list_sessions(cred=Depends(who)) -> dict[str, Any]:
        principal = str(cred.principal.principal_id)
        return {
            "sessions": [
                {
                    "sessionId": str(live.session_id),
                    "running": live.running,
                    "events": len(live.session.events),
                }
                for live in registry.visible_to(principal)
            ]
        }

    @router.post("/sessions", status_code=201)
    async def create_session(cred=Depends(who)) -> dict[str, Any]:
        live = await _open_session(root, registry, cred, workspace_root)
        return {"sessionId": str(live.session_id)}

    @router.get("/sessions/{session_id}")
    async def session_detail(session_id: str, cred=Depends(who)) -> dict[str, Any]:
        live = _owned(registry, session_id, cred)
        return {
            "sessionId": str(live.session_id),
            "running": live.running,
            "events": len(live.session.events),
            "pendingApprovals": [
                {
                    "requestId": p.request.request_id,
                    "tool": p.request.tool_name,
                    "arguments": p.request.arguments_json,
                    "reason": p.request.reason,
                }
                for p in registry.pending_approvals(live.session_id)
            ],
        }

    # -- tasks -------------------------------------------------------------
    @router.post("/tasks", status_code=202)
    async def start_task(body: dict[str, Any], cred=Depends(who)) -> dict[str, Any]:
        """Start a ticket. **202, not 200**, for the same reason a turn is 202:
        the task blocks on approvals that arrive on other requests, so holding
        this one open would make them undeliverable.

        The task id *is* the session id. There is no second identity to keep in
        step, and the event stream a client already knows how to follow is the
        task's timeline — `GET /tasks/{id}` reads the same log.
        """
        ticket_id = body.get("ticketId", body.get("ticket_id"))
        try:
            task_id = await tasks.start(cred, "" if ticket_id is None else str(ticket_id))
        except TaskRefused as exc:
            raise ApiError(exc.status, str(exc)) from exc
        return {"accepted": True, "taskId": task_id, "sessionId": task_id}

    @router.get("/tasks")
    async def list_tasks(cred=Depends(who)) -> dict[str, Any]:
        """The tasks this process is running. Not a durable listing: the store's
        summaries do not carry a principal, and an unscoped list is worse than a
        short one that is honest about its scope."""
        views = await tasks.live_views(str(cred.principal.principal_id))
        return {"tasks": [view.to_payload() for view in views]}

    @router.get("/tasks/{task_id}")
    async def task_detail(task_id: str, cred=Depends(who)) -> dict[str, Any]:
        """A task's state, reconstructed from the session log.

        Works after a restart because the log is durable, and answers 404 rather
        than 403 for a task that exists and is not yours — the same rule the
        session endpoints follow, so the API cannot be used to probe for ids.
        """
        view = await tasks.view(task_id, str(cred.principal.principal_id))
        if view is None:
            raise ApiError(404, f"no task {task_id!r}")
        return view.to_payload()

    # -- turns -------------------------------------------------------------
    @router.post("/sessions/{session_id}/messages", status_code=202)
    async def post_message(
        session_id: str, body: ChatRequest, cred=Depends(who)
    ) -> dict[str, Any]:
        """Start a turn and return immediately.

        **202, not 200.** The turn must not run inside this request: it will
        block on an approval that arrives on another one, and holding this
        connection open would make that approval undeliverable. The client
        follows the turn on the event stream.
        """
        live = _owned(registry, session_id, cred)
        # The busy check comes first, and `start_turn` takes a *factory* rather
        # than a coroutine. Building the coroutine eagerly would leave it
        # un-awaited whenever the check refused — a `RuntimeWarning` at best, and
        # a coroutine that never ran at worst, which is the kind of thing that
        # gets explained away as noise.
        if live.running:
            raise ApiError(409, f"session {session_id} already has a turn in flight")
        # Preparation happens *here*, not in the background task. It is cheap and
        # deterministic, and a failure in it would otherwise leave a turn that
        # never starts: no events, no `turn/end`, and a client waiting on a
        # stream that will never say anything. Failing while the caller is still
        # listening turns a silent hang into a 500.
        turn = _prepare_chat(root, live, body.text, project)
        try:
            registry.start_turn(live, lambda: _run_chat_turn(live, turn))
        except SessionStopping as exc:
            # 503 rather than 409: nothing about this session is busy, and a
            # client that retried against another replica could succeed.
            raise ApiError(503, str(exc)) from exc
        return {"accepted": True, "sessionId": str(live.session_id)}

    @router.get("/sessions/{session_id}/events")
    async def events(
        session_id: str,
        cred=Depends(who),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        after: int | None = Query(default=None),
    ):
        """Stream the session log.

        The cursor is the log's ``seq``. A browser reconnecting after a dropped
        connection sends ``Last-Event-ID`` automatically, so resuming costs
        nothing to implement — and because the cursor is durable, a reconnect
        after a *server* restart replays from storage too.
        """
        live = _owned(registry, session_id, cred)
        cursor = _cursor(last_event_id, after)

        async def stream():
            if metrics is not None:
                metrics.counter(
                    "forgepilot_streams_opened_total", "Event streams opened."
                )
            try:
                async for frame in tail_session(
                    live.session, after=cursor, stopping=registry.stopping
                ):
                    yield frame
            except LostStream as exc:
                # Say so rather than closing silently: the client's cursor is
                # still valid, so reconnecting replays exactly what it missed.
                yield {"event": "stream/lost", "data": str(exc)}
            finally:
                if metrics is not None:
                    metrics.counter(
                        "forgepilot_streams_closed_total", "Event streams closed."
                    )

        return EventSourceResponse(stream())

    # -- approvals ---------------------------------------------------------
    @router.post("/approvals/{request_id}")
    async def answer(request_id: str, body: Decision, cred=Depends(who)) -> dict[str, Any]:
        """Answer one question, once.

        Authorisation is by *ownership of the question*: the request must belong
        to a session this principal owns, or it is not theirs to answer.
        """
        outcome = _outcome(body.decision)
        pending = registry.approvals.get(request_id)
        if pending is None:
            # Not an error worth hiding behind a 404 either way — but a client
            # racing a timeout needs to know its answer landed nowhere.
            return {"answered": False, "reason": "no such pending approval"}
        _owned(registry, str(pending.request.session_id), cred)
        return {"answered": registry.answer(request_id, outcome)}

    # -- the review surface, straight from M9 ------------------------------
    @router.post("/review")
    async def review(body: dict[str, Any], cred=Depends(who)) -> JSONResponse:
        """Render a change narrative. Reuses the review mode rather than a
        second implementation, so the browser and the Markdown cannot disagree."""
        from forgepilot.orchestration.modes.review import build_review
        from forgepilot.runtime.review import render_json

        diff = str(body.get("diff", ""))
        if not diff.strip():
            raise ApiError(400, "a diff is required")
        narrative = await build_review(
            diff,
            title=str(body.get("title", "")),
            llm=None,
            use_model=False,
            ticket_id=body.get("ticketId"),
        )
        return JSONResponse(render_json(narrative))

    return router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cursor(last_event_id: str | None, after: int | None) -> int:
    """Resolve the resume point, preferring the header a browser sends on its own.

    A malformed cursor is a 400 rather than a silently-restarted stream: a client
    that asks for "everything from position banana" and gets the whole log back
    would have no way to know its cursor was ignored.
    """
    if after is not None:
        return max(0, after)
    if last_event_id is None:
        return 0
    try:
        return max(0, int(last_event_id))
    except ValueError:
        raise ApiError(400, f"Last-Event-ID {last_event_id!r} is not a sequence number") from None


def _outcome(decision: str) -> ApprovalOutcome:
    if decision == _ALLOW:
        return ApprovalOutcome.ALLOWED_ONCE
    if decision == _DENY:
        return ApprovalOutcome.REJECTED
    raise ApiError(
        400,
        f"unknown decision {decision!r}; expected {_ALLOW!r} or {_DENY!r}. "
        f"There is no 'always allow': a remembered grant is a permission, which "
        f"belongs to policy, not to a one-shot answer.",
    )


def _owned(registry: SessionRegistry, session_id: str, cred: Any):
    try:
        return registry.get(SessionId(session_id), principal=str(cred.principal.principal_id))
    except SessionNotFound as exc:
        # 404 for "not yours" too: a 403 would confirm that the id exists.
        raise ApiError(404, f"no session {session_id!r}") from exc


async def _open_session(root: Any, registry: SessionRegistry, cred: Any, workspace_root: Path | None):
    from forgepilot.runtime.assembly import assemble_session

    session_id = SessionId(_new_session_id())
    runtime = await assemble_session(
        root,
        cred.constraints,
        session_id=session_id,
        workspace_root=workspace_root,
        principal=cred.principal,
    )
    return registry.add(runtime, principal=str(cred.principal.principal_id))


def _new_session_id() -> str:
    from datetime import datetime
    from uuid import uuid4

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"session-{stamp}-{uuid4().hex[:8]}"


def _prepare_chat(root: Any, live: Any, text: str, project: Any) -> Any:
    """Assemble the mode's context synchronously, so a failure is a response."""
    from forgepilot.orchestration.modes.chat import prepare_chat_turn

    runtime = live.runtime
    return prepare_chat_turn(
        question=text,
        project=project,
        constraints=runtime.constraints,
        workspace_root=runtime.pipeline.workspace_root,
    )


async def _run_chat_turn(live: Any, turn: Any) -> None:
    """Run one prepared chat turn in the background.

    Everything below this line already exists — assemble the context, hand it to
    the same loop the CLI uses — because a second path through the runtime is a
    second thing to keep correct.
    """
    from forgepilot.seams.agent_loop import AgentSpec, UserInput

    runtime = live.runtime
    root = runtime.root
    spec = AgentSpec(
        agent_id=AgentId(f"http-{live.session_id}"),
        session_id=live.session_id,
        mode=Mode.CHAT,
        constraints=runtime.constraints,
        system_sections=turn.system_texts,
    )
    agent = root.agents.build("forgepilot", spec, runtime.scope.ctx).create(spec)
    # `run_turn` closes the turn in the log with a reason even when it raises, so
    # the stream always terminates and the client is never left waiting on a turn
    # that quietly stopped existing.
    await agent.run_turn(UserInput(text=turn.question))
