"""Coordination seam — service name ``coordination``.

One process per state directory was a *safety* property: ``runtime/serve_lock.py``
held a byte-range lock so that two servers could not interleave a session's log.
Multi-replica gives that up, and exactly two things have to be provided by
something shared instead.

**One runner per session.** Sequence numbers are assigned by
:class:`~forgepilot.runtime.session.Session`, not by the store, because a buffered
session must know an event's ``seq`` before flushing (a model-visible message
records ``source_event_seq`` and that is checked against the log). Two replicas
running the same session would assign the same number twice and the log would be
wrong in a way no reader can detect. A *lease* with a monotonic ``fencing``
number says which replica owns a session; the store's own per-session lock is
what enforces the exclusion today, and the fencing number is what a future
shared store (Postgres, Redis) will use to reject a stale writer.

**An approval answered anywhere.** The replica that runs a turn is not
necessarily the replica whose HTTP request carries the human's answer. A
*mailbox* — a pending record, an answer record written once — lets any replica
answer, and lets the waiting replica find out by polling. Polling rather than
pub/sub because a filesystem has no notifications worth the name, and a 100 ms
poll is a latency the UI already has.

Deliberately **not** here: turn state, session metadata, or the log itself. Those
live in the session store, which every replica already reads. This seam carries
only what has to be decided by exactly one replica at a time, or answered by a
different one than asked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from forgepilot.seams.approval import ApprovalOutcome, ApprovalRequest
from forgepilot.seams.refs import SessionId

__all__ = [
    "SERVICE_NAME",
    "Coordinator",
    "Lease",
    "LeaseRefused",
    "CoordinationUnavailable",
]

SERVICE_NAME = "coordination"


class LeaseRefused(RuntimeError):
    """Another replica holds this session. Not an error about the session."""


class CoordinationUnavailable(RuntimeError):
    """The shared state cannot be reached, so nothing may be decided."""


class MaintenanceInProgress(RuntimeError):
    """A backup holds the deployment: no new work starts until it ends.

    Raised rather than silently writing. A backup of a live log is the one operation
    whose damage is invisible until a restore is needed, so the writer is told to
    retry instead of being allowed to race it.
    """


@dataclass(frozen=True, slots=True)
class Lease:
    """Ownership of one session for a while, with a number that only grows.

    ``expires_at`` is epoch seconds on the *wall* clock, because the replicas do
    not share a monotonic clock — the only synchronized thing they have is that
    one. ``fencing`` increases on every successful acquisition of the same
    session, so a writer holding a stale lease can be told apart from the current
    one.
    """

    session_id: SessionId
    holder: str
    fencing: int
    expires_at: float


@runtime_checkable
class Coordinator(Protocol):
    """The little that cannot be process-local.

    Every method may raise :class:`CoordinationUnavailable`; callers must treat
    that as a refusal to decide, never as "no one holds it".
    """

    async def acquire(self, session_id: SessionId, *, holder: str, ttl_s: float) -> Lease | None:
        """Take the lease, or return ``None`` when a live one exists."""

    async def renew(self, lease: Lease, *, ttl_s: float) -> Lease | None:
        """Extend a lease this caller still holds. ``None`` means it was lost."""

    async def release(self, lease: Lease) -> None: ...

    async def holder(self, session_id: SessionId) -> str | None:
        """Who holds a *live* lease right now. ``None`` means nobody does."""

    # -- approvals ---------------------------------------------------------
    async def post_approval(self, request: ApprovalRequest, *, expires_at: float) -> None:
        """Publish a question so any replica can answer it. Idempotent by id."""

    async def answer_approval(
        self, request_id: str, outcome: ApprovalOutcome, *, principal: str
    ) -> bool:
        """Answer once. ``False`` means it was already answered (or unknown)."""

    async def wait_approval(self, request_id: str, *, timeout_s: float) -> ApprovalOutcome | None:
        """Poll until answered or the timeout expires. ``None`` means silent."""

    async def pending_approvals(self, session_id: SessionId) -> tuple[ApprovalRequest, ...]:
        """Questions on this session that nobody has answered yet."""

    async def join_writers(self, *, holder: str, ttl_s: float) -> Lease | None:
        """Register this process as a writer, or ``None`` while a backup holds it.

        A writer is not a session: it is *this process may append*. The set exists so
        a backup can ask "is anybody writing?" without enumerating sessions, and can
        then be refused instead of racing a log it cannot snapshot.
        """
        ...

    async def leave_writers(self, lease: Lease) -> None: ...

    async def writers(self) -> tuple[str, ...]:
        """Holders with a live writer heartbeat, for the message a refusal prints."""
        ...

    async def enter_maintenance(self, *, holder: str, ttl_s: float) -> Lease | None:
        """Take the deployment, or ``None`` while any writer heartbeat is live."""
        ...

    async def leave_maintenance(self, lease: Lease) -> None: ...

    async def pending_request(self, request_id: str) -> ApprovalRequest | None:
        """One question by id, or ``None`` — needed to authorise an answer.

        The replicas that answer are usually not the one that asked, so the
        question has to travel without its asker's memory attached.
        """
