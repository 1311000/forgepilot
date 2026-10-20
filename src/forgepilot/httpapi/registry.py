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
from dataclasses import dataclass, field
from typing import Any

from forgepilot.seams.approval import ApprovalOutcome, ApprovalRequest
from forgepilot.seams.refs import SessionId

__all__ = [
    "PendingApproval",
    "SessionBusy",
    "SessionNotFound",
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


class SessionStopping(RuntimeError):
    """The process is shutting down, so it accepts no new work.

    A distinct error from :class:`SessionBusy` because it is not a property of
    the session: retrying the same request against a live sibling replica would
    succeed, and a client that saw 409 would reasonably conclude otherwise.
    """


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
    approval_timeout: float = 300.0
    sessions: dict[SessionId, LiveSession] = field(default_factory=dict)
    approvals: dict[str, PendingApproval] = field(default_factory=dict)
    #: Set once, at shutdown. Read by the SSE tail so a stream ends *now* rather
    #: than when the next event happens to arrive.
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    #: Optional counter sink. Present so the numbers come from the object that
    #: owns the truth rather than from a call site that could be forgotten.
    metrics: Any = None

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
    def start_turn(self, live: LiveSession, factory: Any) -> asyncio.Task:
        """Run a turn in the background. Refuses if one is already in flight.

        Takes a zero-argument factory rather than a coroutine, so the refusal
        path cannot leave an un-awaited coroutine behind: refusing a turn must
        not also create one.
        """
        if live.running:
            raise SessionBusy(str(live.session_id))
        if self.stopping.is_set():
            raise SessionStopping("the server is shutting down")
        task = asyncio.create_task(factory())
        live.task = task
        if self.metrics is not None:
            self.metrics.counter("forgepilot_turns_started_total", "Turns started, chat and task.")
        # Retrieve the exception. The loop already closed the turn in the log —
        # this is about the *process*: an unretrieved task exception is reported
        # as a warning at collection time, or not at all, and a background turn
        # that died is exactly the thing nobody would otherwise notice.
        task.add_done_callback(_report_failure(live))
        return task

    def stop(self) -> None:
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
            self.answer(request_id, ApprovalOutcome.UNAVAILABLE)

    async def close(self) -> None:
        """Cancel in-flight turns and close every session."""
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
        try:
            return await asyncio.wait_for(pending.future, timeout=self.approval_timeout)
        except (TimeoutError, asyncio.CancelledError):
            return ApprovalOutcome.UNAVAILABLE
        finally:
            self.approvals.pop(request.request_id, None)

    def answer(self, request_id: str, outcome: ApprovalOutcome) -> bool:
        """Resolve a pending question. Returns whether one was waiting.

        One-shot: the future is resolved once and the entry is dropped, so a
        replayed POST cannot grant a second call. There is no "remember this"
        outcome, because a remembered grant is a *permission*, which is a
        different concept owned by ``policy``.
        """
        pending = self.approvals.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.answered = True
        pending.future.set_result(outcome)
        return True

    def pending_approvals(self, session_id: SessionId) -> tuple[PendingApproval, ...]:
        return tuple(p for p in self.approvals.values() if p.request.session_id == session_id)

    def install(self) -> None:
        """Become the approval service's answerer.

        One answerer for the process, routing by ``request_id``. The alternative
        — swapping the answerer per turn — would be a data race between two
        sessions asking at the same time, and the loser's question would be
        answered by the other session's human.
        """
        self.root.approval.set_answerer(self.ask)
