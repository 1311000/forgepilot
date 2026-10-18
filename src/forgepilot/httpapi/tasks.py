"""Task mode over HTTP: start a ticket, then read its state back from the log.

**No new execution path.** `start()` prepares a session the way `--task` does and
hands the ticket to `run_task`; everything reported afterwards is read from the
session log, which is why a server that restarts can still answer "what happened
to this ticket?" without having kept anything in memory.

Two places where the API has to do something the CLI does not, and both exist so a
browser never shows a task that looks alive and never says anything:

- **The ticket is fetched before the session exists.** A session created for an id
  the source does not have would start, fail to plan, and leave a log whose only
  content is a refusal. Fetching first turns that into a 404 that costs nothing.
- **A plan that is not ready writes the refusal itself.** `plan_task` does not take
  the session (M16 chose that so its signature — and its fifteen call sites — did
  not move), so `AWAITING_INFO` would leave no event at all. The service appends
  one, in the same shape `run_task` uses for its own states.

The log is the durable half and the registry is the live half. A task in flight is
served from the registry, because its events include the ones still buffered; a
finished one — or one whose process is gone — is served from the store with
`recover=False`, because recovering a tail *writes*, and a reader of history is not
allowed to.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forgepilot.errors import ForgePilotError
from forgepilot.httpapi.registry import (
    SessionNotFound,
    SessionRegistry,
    SessionStopping,
)
from forgepilot.seams.refs import SessionId, TicketId
from forgepilot.seams.session import EventKind

__all__ = ["RepoView", "TaskRefused", "TaskService", "TaskView"]


class TaskRefused(ForgePilotError):
    """A task that could not be started, with the status the boundary should use."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class RepoView:
    """What the log says happened to one repository."""

    slug: str
    state: str
    reason: str
    branch: str = ""
    commit: str | None = None
    changed: tuple[str, ...] = ()
    pull_request: str | None = None
    checks: tuple[dict[str, Any], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "repo": self.slug,
            "state": self.state,
            "reason": self.reason,
            "branch": self.branch,
            "commit": self.commit,
            "changed": list(self.changed),
            "pullRequest": self.pull_request,
            "checks": [dict(check) for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class TaskView:
    """One task, as the log and (when it is running) the registry describe it."""

    task_id: str
    session_id: str
    ticket_id: str
    state: str
    reason: str
    live: bool
    interrupted: bool
    repositories: tuple[RepoView, ...]
    pending_approvals: tuple[dict[str, Any], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "sessionId": self.session_id,
            "ticketId": self.ticket_id,
            "state": self.state,
            "reason": self.reason,
            "live": self.live,
            # A task whose log stops while it was running. Said as its own fact
            # rather than as a state, because the state it reached is still true —
            # what is missing is the ending, not the work.
            "interrupted": self.interrupted,
            "repositories": [repo.to_payload() for repo in self.repositories],
            "pendingApprovals": [dict(item) for item in self.pending_approvals],
        }


class TaskService:
    """Starts tasks and reads them back. Owns no timeline of its own."""

    def __init__(
        self,
        root: Any,
        registry: SessionRegistry,
        *,
        project: Any,
        workspace_root: Path | None,
        project_configured: bool,
        metrics: Any = None,
    ) -> None:
        self._root = root
        self._registry = registry
        self._project = project
        self._workspace_root = workspace_root
        self._project_configured = project_configured
        self._metrics = metrics
        # Which ticket a live session is for, before its first event exists. The
        # durable answer is the `ticket` field of the opening `task/state`; this
        # only covers the seconds between `POST /tasks` and that event.
        self._started: dict[SessionId, str] = {}

    # -- starting ----------------------------------------------------------
    async def start(self, cred: Any, ticket_id: str) -> str:
        """Prepare a task and start it in the background. Returns the task id."""
        if not self._project_configured:
            raise TaskRefused(
                409,
                "task mode needs a project: no project.yaml was found. Start the "
                "server with --workspace pointing at a project directory, or use "
                "chat mode.",
            )
        wanted = ticket_id.strip()
        if not wanted:
            raise TaskRefused(400, "a ticket id is required")

        try:
            ticket = await self._root.tickets.fetch(TicketId(wanted))
        except KeyError as exc:
            # The provider's own message names the tickets it does have.
            detail = str(exc.args[0]) if exc.args else str(exc)
            raise TaskRefused(404, detail) from None

        from forgepilot.runtime.assembly import assemble_session

        try:
            runtime = await assemble_session(
                self._root,
                cred.constraints,
                workspace_root=self._workspace_root,
                principal=cred.principal,
            )
        except ForgePilotError as exc:
            # A session that cannot be opened is a configuration refusal, not a
            # server fault: the message is the fix (a missing workspace root, a
            # profile without a policy).
            raise TaskRefused(409, f"cannot open a session for this task: {exc}") from exc

        live = self._registry.add(runtime, principal=str(cred.principal.principal_id))
        self._started[live.session_id] = wanted
        try:
            self._registry.start_turn(live, lambda: self._run(live, ticket, cred))
        except SessionStopping as exc:
            # The session was assembled a moment too late, and it will never run.
            # Dropping it keeps `GET /sessions` honest about there being work.
            self._started.pop(live.session_id, None)
            self._registry.sessions.pop(live.session_id, None)
            raise TaskRefused(503, str(exc)) from exc
        if self._metrics is not None:
            self._metrics.counter(
                "forgepilot_tasks_started_total", "Tasks accepted, one per ticket."
            )
        return str(live.session_id)

    async def _run(self, live: Any, ticket: Any, cred: Any) -> None:
        """Plan, then deliver. Runs as the session's turn, not inside a request."""
        from forgepilot.orchestration import ClassifierGateway
        from forgepilot.orchestration.modes.task import plan_task, run_task
        from forgepilot.runtime.assembly import usage_tracker
        from forgepilot.runtime.loop.turn import DEFAULT_MAX_STEPS

        root = self._root
        runtime = live.runtime
        try:
            gateway = ClassifierGateway(root.classifier, session=runtime.session)
            plan = await plan_task(
                ticket=ticket,
                project=self._project,
                constraints=runtime.constraints,
                registry=root.tools,
                classifier=gateway,
                workspace_root=self._workspace_root,
            )
            if not plan.ready:
                # Nothing else in this path writes for a plan that is not ready, so
                # without this the log would hold a session that started and then
                # said nothing, and the panel would wait forever.
                await _append_state(
                    runtime,
                    {
                        "state": str(plan.state),
                        "reason": plan.reason,
                        "ticket": str(ticket.id),
                    },
                )
                return

            await run_task(
                plan=plan,
                project=self._project,
                session=runtime.session,
                llm=root.llm,
                tools=root.tools,
                vcs=root.vcs,
                workspace=root.workspace,
                sandbox=root.sandbox,
                constraints=runtime.constraints,
                pipeline=runtime.pipeline,
                tracker=usage_tracker(runtime),
                model=getattr(root.llm, "model", "?"),
                policy=root.policy,
                approval=root.approval,
                principal=cred.principal,
                compactor=getattr(root, "compaction", None),
                memory=getattr(root, "memory", None),
                max_steps=getattr(getattr(root, "agentLoop", None), "max_steps", None)
                or DEFAULT_MAX_STEPS,
            )
        except Exception as exc:  # noqa: BLE001 - the point is to record, not to hide
            # A background task cannot raise to anyone: the HTTP response was sent
            # when it started. If the log does not carry the failure, the client
            # sees a task that simply stopped, which is the one thing M15 was about.
            await _append_state(
                runtime,
                {
                    "state": "failed",
                    "reason": f"the task stopped with {type(exc).__name__}: {exc}",
                },
            )

    # -- reading -----------------------------------------------------------
    async def view(self, session_id: str, principal: str) -> TaskView | None:
        """One task, or `None` when there is no such task *for this principal*."""
        sid = SessionId(session_id)
        try:
            live = self._registry.get(sid, principal=principal)
        except SessionNotFound:
            live = None

        if live is not None:
            return self._from_events(
                live.session.events,
                sid=sid,
                live=True,
                started=sid in self._started,
                ticket_id=self._started.get(sid, ""),
                pending=self._pending(live),
            )

        store = getattr(self._root, "sessionPersistence", None)
        if store is None:
            return None
        try:
            header = await store.header(sid)
        except FileNotFoundError:
            return None
        # The header carries the principal, so ownership survives a restart
        # without a second index. `SessionSummary` does not, which is why the
        # listing below is live-only.
        if header.principal != principal:
            return None
        events = [event async for event in store.read(sid)]
        return self._from_events(events, sid=sid, live=False, started=False, ticket_id="")

    async def live_views(self, principal: str) -> tuple[TaskView, ...]:
        """The tasks this process is running, for this principal.

        Deliberately not a durable listing: the store's summaries carry no
        principal, so a list built from them could not be scoped, and one that is
        not scoped is worse than one that is absent.
        """
        views: list[TaskView] = []
        for live in self._registry.visible_to(principal):
            view = self._from_events(
                live.session.events,
                sid=live.session_id,
                live=True,
                started=live.session_id in self._started,
                ticket_id=self._started.get(live.session_id, ""),
                pending=self._pending(live),
            )
            if view is not None:
                views.append(view)
        return tuple(views)

    def _pending(self, live: Any) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "requestId": pending.request.request_id,
                "tool": pending.request.tool_name,
                "arguments": pending.request.arguments_json,
                "reason": pending.request.reason,
            }
            for pending in self._registry.pending_approvals(live.session_id)
        )

    def _from_events(
        self,
        events: Any,
        *,
        sid: SessionId,
        live: bool,
        started: bool,
        ticket_id: str,
        pending: tuple[dict[str, Any], ...] = (),
    ) -> TaskView | None:
        """Fold the log into a view. Returns `None` when this is not a task."""
        ticket = ticket_id
        order: list[str] = []
        deliveries: dict[str, dict[str, Any]] = {}
        checks: dict[str, list[dict[str, Any]]] = {}
        closing: dict[str, Any] | None = None
        seen = False

        for event in events:
            if event.kind == EventKind.TASK_STATE:
                data = event.data
                seen = True
                if not ticket and data.get("ticket"):
                    ticket = str(data["ticket"])
                if "repo" in data:
                    slug = str(data["repo"])
                    if slug not in deliveries:
                        order.append(slug)
                    deliveries[slug] = data
                else:
                    # No `repo`: the task's own word about itself. The opening
                    # `running` event looks the same as the closing verdict — both
                    # name the repositories — so "the last one wins" is the rule,
                    # and a log that ends on the opening event is `interrupted`
                    # rather than a task that is somehow still running.
                    closing = data
            elif event.kind == EventKind.TASK_CHECK:
                data = event.data
                if data.get("repo") is not None:
                    checks.setdefault(str(data["repo"]), []).append(
                        {
                            "command": data.get("command", ""),
                            "passed": data.get("passed"),
                            "reason": data.get("reason", ""),
                        }
                    )

        if not seen and not started:
            # A session that was never a task — chat, or a stream someone opened.
            return None
        if not seen:
            # Started, nothing written yet. `queued` is the state a task has before
            # its first event, and it is honest about not knowing more.
            return TaskView(
                task_id=str(sid),
                session_id=str(sid),
                ticket_id=ticket,
                state="queued",
                reason="",
                live=True,
                interrupted=False,
                repositories=(),
                pending_approvals=pending,
            )

        repos = tuple(
            RepoView(
                slug=slug,
                state=str(deliveries[slug].get("state", "")),
                reason=str(deliveries[slug].get("reason", "")),
                branch=str(deliveries[slug].get("branch") or ""),
                commit=deliveries[slug].get("commit"),
                changed=tuple(deliveries[slug].get("changed") or ()),
                pull_request=deliveries[slug].get("pullRequest"),
                checks=tuple(checks.get(slug, ())),
            )
            for slug in order
        )

        verdict = None
        if closing is not None and str(closing.get("state", "")) not in {"", "running"}:
            # A task's own word about itself: `awaiting_info`, or the closing
            # aggregate written once the deliveries are done.
            verdict = closing
        if verdict is not None:
            state = str(verdict.get("state", ""))
            reason = str(verdict.get("reason", ""))
        elif repos:
            # A log written before the aggregate existed, or one that stopped after
            # a delivery: the last delivery is the last thing it knows.
            state = repos[-1].state
            reason = repos[-1].reason
        elif closing is not None:
            state = str(closing.get("state", "running"))
            reason = str(closing.get("reason", ""))
        else:
            state = "running"
            reason = ""

        return TaskView(
            task_id=str(sid),
            session_id=str(sid),
            ticket_id=ticket,
            state=state,
            reason=reason,
            live=live,
            # "Nobody is running this, and the log still says running." Both halves
            # are needed: a live task legitimately has not finished, and a delivered
            # one legitimately has no closing aggregate in an older log.
            interrupted=not live and state == "running",
            repositories=repos,
            pending_approvals=pending,
        )


async def _append_state(runtime: Any, data: dict[str, Any]) -> None:
    """Record a state the runner did not, and make it durable."""
    await runtime.session.append(EventKind.TASK_STATE, data)
    await runtime.session.flush()
