"""Live sessions, their in-flight turns, and the approvals waiting on a human.

Three lifetimes meet in this module, and keeping them apart is most of what it
does:

- a **credential** is checked per request (:mod:`forgepilot.httpapi.deps`);
- a **session** is long-lived — it outlives the POST that created it, so its
  authority scope is opened once, by ``assemble_session``, and not per request;
- a **turn** is a background task, because it must not hold an HTTP connection
  open: it will block on an approval that arrives on a *different* request.

That last one is the reason this module exists at all. A turn that ran inside its
SSE handler would occupy the connection it is streaming to, and the approval
response — the very thing it is waiting for — could never be delivered. The
deadlock would look like a hang, not an error.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from forgepilot.seams.approval import ApprovalOutcome, ApprovalRequest
from forgepilot.seams.coordination import MaintenanceInProgress
from forgepilot.seams.refs import SessionId

__all__ = [
    "PendingApproval",
    "SessionBusy",
    "SessionNotFound",
    "SessionOwnedElsewhere",
    "SessionRegistry",
    "SessionStopping",
]


def _report_failure(live: LiveSession):
    """A done-callback that consumes the task's exception, loudly.

    The log already records a failed turn — the loop closes it with reason
    ``error`` — but a *process* that swallows the traceback makes a broken turn
    indistinguishable from one that simply produced nothing interesting.
    """

    def callback(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            import logging

            logging.getLogger("forgepilot.httpapi").error(
                "turn on session %s failed: %s: %s",
                live.session_id,
                type(error).__name__,
                error,
                exc_info=error,
            )

    return callback


class SessionNotFound(KeyError):
    """No such live session — or not one this caller may see.

    Deliberately one error for both. Telling an unauthorised caller "that id
    exists but is not yours" leaks the existence of other people's sessions.
    """


class SessionBusy(RuntimeError):
    """A turn is already running on this session.

    Refused rather than queued: one session has one runner, and two overlapping
    turns would interleave in the log — the ordering authority the whole design
    rests on.
    """


class SessionOwnedElsewhere(RuntimeError):
    """Another replica holds this session's lease.

    Distinct from :class:`SessionBusy` — which says *this* process already has a
    turn in flight — because the honest answer to the client is different: the
    work exists, it is just not this replica's to run. Without a shared lease the
    second replica would start a second turn on one log, and two writers would
    assign the same ``seq`` twice.
    """


class SessionStopping(RuntimeError):
    """The process is shutting down, so it accepts no new work.

    A distinct error from :class:`SessionBusy` because it is not a property of
    the session: retrying the same request against a live sibling replica would
    succeed, and a client that saw 409 would reasonably conclude otherwise.
    """


#: How long an approval waits before it is treated as a refusal. Named because
#: `deploy/alerts.yml` must fire *before* this elapses; a threshold above it can never
#: fire at all, and a guard asserts the relationship against this constant.
DEFAULT_APPROVAL_TIMEOUT_S = 300.0


@dataclass
class PendingApproval:
    """One question waiting for an answer, and the future that carries it."""

    request: ApprovalRequest
    future: asyncio.Future
    answered: bool = False


@dataclass
class LiveSession:
    """A session runtime plus what the HTTP layer knows about it."""

    runtime: Any
    #: The credential that created it. Ownership is checked against this rather
    #: than against a mutable field on the runtime.
    principal: str
    task: asyncio.Task | None = None

    @property
    def session(self) -> Any:
        return self.runtime.session

    @property
    def session_id(self) -> SessionId:
        return self.runtime.session.header.session_id

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


@dataclass
class SessionRegistry:
    """Everything the HTTP surface needs to keep tabs on live work.

    In-process state, and knowingly so: this makes the API single-process. Two
    replicas would each hold half the sessions and neither could stream the
    other's. Sticky routing or a real pub/sub is the answer, and neither is
    built — see the M10 notes in CLAUDE.md.
    """

    root: Any
    #: How long a question waits for a human before it is treated as silence.
    approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT_S
    sessions: dict[SessionId, LiveSession] = field(default_factory=dict)
    approvals: dict[str, PendingApproval] = field(default_factory=dict)
    #: Set once, at shutdown. Read by the SSE tail so a stream ends *now* rather
    #: than when the next event happens to arrive.
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    #: Optional counter sink. Present so the numbers come from the object that
    #: owns the truth rather than from a call site that could be forgotten.
    metrics: Any = None
    #: The shared half (P16). ``None`` keeps the in-process behaviour: one
    #: replica, a lock on the state directory, and approvals answered by this
    #: process only.
    coordinator: Any = None
    #: How this replica names itself in a lease. The pid is not enough — two
    #: replicas can run under the same pid on different hosts.
    holder: str = ""
    lease_ttl_s: float = 30.0
    renew_s: float = 10.0
    _leases: dict[SessionId, Any] = field(default_factory=dict)
    _heartbeats: dict[SessionId, asyncio.Task] = field(default_factory=dict)
    _writer_lease: Any = None
    _writer_heartbeat: asyncio.Task | None = None

    # -- sessions ----------------------------------------------------------
    def add(self, runtime: Any, *, principal: str) -> LiveSession:
        live = LiveSession(runtime=runtime, principal=principal)
        self.sessions[live.session_id] = live
        return live

    def get(self, session_id: SessionId, *, principal: str) -> LiveSession:
        """Fetch a session, refusing anything this principal does not own."""
        live = self.sessions.get(session_id)
        if live is None or live.principal != principal:
            raise SessionNotFound(str(session_id))
        return live

    def visible_to(self, principal: str) -> tuple[LiveSession, ...]:
        return tuple(s for s in self.sessions.values() if s.principal == principal)

    # -- turns -------------------------------------------------------------
    async def start_turn(self, live: LiveSession, factory: Any) -> asyncio.Task:
        """Run a turn in the background. Refuses if one is already in flight.

        Takes a zero-argument factory rather than a coroutine, so the refusal
        path cannot leave an un-awaited coroutine behind: refusing a turn must
        not also create one.

        With a coordinator, the turn runs under a **lease** — see
        :mod:`forgepilot.seams.coordination` for why the log cannot have two
        writers. The lease is renewed while the turn runs and released when it
        ends, including when it ends by raising.
        """
        if live.running:
            raise SessionBusy(str(live.session_id))
        if self.stopping.is_set():
            raise SessionStopping("the server is shutting down")
        # A backup takes the deployment lease; a writer may not start under it. This
        # is checked here, once, rather than in every store: "may this process append
        # at all" is a fact about the deployment, and the turn is the unit that
        # appends.
        await self._ensure_writer()
        lease = None
        if self.coordinator is not None:
            lease = await self.coordinator.acquire(
                live.session_id, holder=self.holder, ttl_s=self.lease_ttl_s
            )
            if lease is None:
                raise SessionOwnedElsewhere(str(live.session_id))
            self._leases[live.session_id] = lease
        task = asyncio.create_task(factory())
        if lease is not None:
            self._heartbeats[live.session_id] = asyncio.create_task(
                self._keep_the_lease(live.session_id, task)
            )
        live.task = task
        if self.metrics is not None:
            self.metrics.counter("forgepilot_turns_started_total", "Turns started, chat and task.")
        # Retrieve the exception. The loop already closed the turn in the log —
        # this is about the *process*: an unretrieved task exception is reported
        # as a warning at collection time, or not at all, and a background turn
        # that died is exactly the thing nobody would otherwise notice.
        task.add_done_callback(_report_failure(live))
        return task

    async def _ensure_writer(self) -> None:
        """Join the writer set on first use, and keep its heartbeat alive.

        Joining happens on the first turn rather than at construction: a registry
        built in a test, or by a replica that never receives work, must not appear
        as a writer — a backup refused by a process that is not writing would be a
        false refusal, and a false refusal is how a lock gets removed entirely.
        """
        if self._writer_lease is not None or self.coordinator is None:
            return
        joined = getattr(self.coordinator, "join_writers", None)
        if joined is None:
            return
        lease = await joined(holder=self.holder, ttl_s=self.lease_ttl_s)
        if lease is None:
            holders = getattr(self.coordinator, "writers", None)
            live = await holders() if holders is not None else ()
            raise MaintenanceInProgress(
                "a backup holds the deployment"
                + (f" (writers: {', '.join(live)})" if live else "")
                + "; try again when it ends"
            )
        self._writer_lease = lease
        self._writer_heartbeat = asyncio.create_task(self._keep_the_writer())

    async def _keep_the_writer(self) -> None:
        while self._writer_lease is not None:
            await asyncio.sleep(self.renew_s)
            lease = self._writer_lease
            if lease is None:
                return
            renewed = await self.coordinator.renew(lease, ttl_s=self.lease_ttl_s)
            if renewed is None:
                # The lease was taken or expired. Stop pretending to be a writer;
                # the next turn will try to join again and be refused if needed.
                self._writer_lease = None
                return
            self._writer_lease = renewed

    async def _keep_the_lease(self, session_id: SessionId, task: asyncio.Task) -> None:
        """Renew while the turn runs; release when it ends, however it ends."""
        try:
            while not task.done():
                await asyncio.sleep(self.renew_s)
                lease = self._leases.get(session_id)
                if lease is None:
                    return
                renewed = await self.coordinator.renew(lease, ttl_s=self.lease_ttl_s)
                if renewed is None:
                    # The lease was taken from us (an expired clock, a paused
                    # process). There is no safe way to continue writing, so the
                    # turn is stopped rather than left to interleave.
                    task.cancel()
                    return
                self._leases[session_id] = renewed
        finally:
            lease = self._leases.pop(session_id, None)
            self._heartbeats.pop(session_id, None)
            if lease is not None:
                await self.coordinator.release(lease)

    async def stop(self) -> None:
        await self._leave_writers()
        """Begin shutdown: refuse new turns, and stop waiting on humans.

        Two things have to happen before :meth:`close` cancels anything, because
        a cancellation that arrives while a turn is *blocked on an approval*
        leaves the turn's own `finally` handling to explain a "the server went
        away" that never reached the log. Releasing the questions as
        ``UNAVAILABLE`` first lets the turn take its ordinary refusal path, which
        the log already knows how to record.

        Synchronous on purpose: it is called from the ASGI lifespan, where an
        ``await`` before cancelling would give in-flight turns another chance to
        start yet another approval.
        """
        self.stopping.set()
        for request_id in list(self.approvals):
            await self.answer(request_id, ApprovalOutcome.UNAVAILABLE)

    async def _leave_writers(self) -> None:
        """Give up the writer role, so a waiting backup is not refused by a dead server."""
        heartbeat = self._writer_heartbeat
        lease = self._writer_lease
        self._writer_heartbeat = None
        self._writer_lease = None
        if heartbeat is not None:
            heartbeat.cancel()
        if lease is None or self.coordinator is None:
            return
        leave = getattr(self.coordinator, "leave_writers", None)
        if leave is not None:
            await leave(lease)

    async def close(self) -> None:
        """Cancel in-flight turns and close every session."""
        for heartbeat in list(self._heartbeats.values()):
            heartbeat.cancel()
        self._heartbeats.clear()
        for live in list(self.sessions.values()):
            if live.running:
                live.task.cancel()
        for live in list(self.sessions.values()):
            try:
                await live.runtime.close()
            except Exception:  # noqa: BLE001 - shutdown must not stop halfway
                pass
        self.sessions.clear()

    # -- approvals ---------------------------------------------------------
    async def ask(self, request: ApprovalRequest) -> ApprovalOutcome:
        """The process-wide answerer. Routes to whoever is waiting on this id.

        Registered once against the approval service, because that service holds
        a single answerer — see :meth:`install`.

        **An unanswered question is a refusal.** The timeout is not a policy of
        its own: it exists so that a human who closes the tab produces
        ``UNAVAILABLE`` (which every consumer already treats as "no") rather than
        a turn pinned open forever.
        """
        loop = asyncio.get_running_loop()
        pending = PendingApproval(request=request, future=loop.create_future())
        self.approvals[request.request_id] = pending
        mirror: asyncio.Task | None = None
        if self.coordinator is not None:
            await self.coordinator.post_approval(
                request, expires_at=time.time() + self.approval_timeout
            )
            mirror = asyncio.create_task(self._mirror(request.request_id, pending))
        try:
            return await asyncio.wait_for(pending.future, timeout=self.approval_timeout)
        except (TimeoutError, asyncio.CancelledError):
            return ApprovalOutcome.UNAVAILABLE
        finally:
            if mirror is not None:
                mirror.cancel()
            self.approvals.pop(request.request_id, None)

    async def _mirror(self, request_id: str, pending: PendingApproval) -> None:
        """Resolve the local future when *another* replica answers the question.

        Polling, because a filesystem has no notification worth the name. The
        local answer path stays synchronous-to-the-future, so a question answered
        by the replica that asked it never waits for a poll interval.
        """
        try:
            outcome = await self.coordinator.wait_approval(
                request_id, timeout_s=self.approval_timeout
            )
        except Exception:  # noqa: BLE001 - a broken mailbox must not kill the turn
            return
        if outcome is not None and not pending.future.done():
            pending.future.set_result(outcome)

    async def answer(
        self, request_id: str, outcome: ApprovalOutcome, *, principal: str = "-"
    ) -> bool:
        """Resolve a pending question. Returns whether one was waiting.

        One-shot: the future is resolved once and the entry is dropped, so a
        replayed POST cannot grant a second call. There is no "remember this"
        outcome, because a remembered grant is a *permission*, which is a
        different concept owned by ``policy``.
        """
        answered = False
        pending = self.approvals.get(request_id)
        if pending is not None and not pending.future.done():
            pending.answered = True
            pending.future.set_result(outcome)
            answered = True
        if self.coordinator is not None:
            # Write the shared answer even when this replica was the waiter: the
            # other replicas cannot see this process's future, and an answer that
            # exists only in memory is an answer a restart loses.
            written = await self.coordinator.answer_approval(
                request_id, outcome, principal=principal
            )
            answered = answered or written
        return answered

    async def pending_approvals(self, session_id: SessionId) -> tuple[ApprovalRequest, ...]:
        """Questions on this session nobody has answered, whoever is waiting.

        Returns requests rather than futures: a question another replica is
        waiting on has no future here, and handing out an unresolvable one would
        invite a caller to await it.
        """
        local = tuple(
            p.request for p in self.approvals.values() if p.request.session_id == session_id
        )
        if self.coordinator is None:
            return local
        seen = {request.request_id for request in local}
        remote = await self.coordinator.pending_approvals(session_id)
        return local + tuple(r for r in remote if r.request_id not in seen)

    async def lookup(self, request_id: str) -> ApprovalRequest | None:
        """The question behind an id, from this replica or from the mailbox.

        Needed because authorisation is by *ownership of the question*: a replica
        that is not waiting on it still has to know whose question this is before
        it may answer.
        """
        pending = self.approvals.get(request_id)
        if pending is not None:
            return pending.request
        if self.coordinator is None:
            return None
        request = await self.coordinator.pending_request(request_id)
        return request

    async def active(self, session_id: SessionId) -> bool:
        """Is a replica running this session right now?

        Used where the answer used to be "is it in this process's registry": with
        N replicas, a task that is running elsewhere still is not *interrupted*.
        """
        live = self.sessions.get(session_id)
        if live is not None and live.running:
            return True
        if self.coordinator is None:
            return live is not None
        return await self.coordinator.holder(session_id) is not None

    def install(self) -> None:
        """Become the approval service's answerer.

        One answerer for the process, routing by ``request_id``. The alternative
        — swapping the answerer per turn — would be a data race between two
        sessions asking at the same time, and the loser's question would be
        answered by the other session's human.
        """
        self.root.approval.set_answerer(self.ask)
