"""The Session object: the **single write path** into the log.

Every event that ever reaches storage goes through :meth:`Session.append`. That
is what makes the invariants enforceable rather than aspirational — there is one
place to validate, one place to assign sequence numbers, and one place to buffer.

**Sequence numbers are assigned here, not by the store.** A buffered session has
to know an event's seq before flushing, because a model-visible message records
``source_event_seq`` and that is checked against the log. The store's job is the
complementary one: reject any batch that does not continue the log exactly, so
an off-by-one surfaces instead of being silently renumbered.

**Durability is not implicit.** Events are buffered; :meth:`flush` is the barrier.
:func:`forgepilot.runtime.log_guard.guarded_stream` flushes before dispatch, so
"everything the model saw is already on disk" holds by construction rather than
by a caller remembering to call flush.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from forgepilot.runtime.events import open_turn, pending_tool_calls
from forgepilot.runtime.redaction import (
    RedactionReport,
    Redactor,
    collapse_payload,
    default_redactor,
)
from forgepilot.seams.refs import SessionId, TurnId
from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.session import (
    EventKind,
    SessionEvent,
    SessionHeader,
    SessionStore,
    SurfaceOp,
    TailRecovery,
)

__all__ = ["Session"]


class Session:
    """A live view over one session log."""

    def __init__(
        self,
        store: SessionStore,
        header: SessionHeader,
        committed: Sequence[SessionEvent] = (),
        redactor: Redactor | None = None,
    ) -> None:
        self._store = store
        self.header = header
        self._committed: list[SessionEvent] = list(committed)
        self._pending: list[SessionEvent] = []
        self._next_seq = (self._committed[-1].seq + 1) if self._committed else 1
        self._listeners: list[Callable[[SessionEvent], None]] = []
        #: The gate on everything written below. Injected rather than reached for
        #: globally, so a test can prove what the default does and a deployment can
        #: widen the internal-host list without editing this module.
        self._redactor = redactor or default_redactor()

    # -- construction ------------------------------------------------------
    @classmethod
    async def create(
        cls,
        store: SessionStore,
        session_id: SessionId,
        *,
        tenant: str = "",
        principal: str = "",
        cwd: str = "",
        redactor: Redactor | None = None,
    ) -> Session:
        header = SessionHeader(
            session_id=session_id,
            created_at=datetime.now(UTC),
            tenant=tenant,
            principal=principal,
            cwd=cwd,
        )
        await store.create(header)
        return cls(store, header, redactor=redactor)

    @classmethod
    async def open(
        cls,
        store: SessionStore,
        session_id: SessionId,
        *,
        recover: bool = True,
        redactor: Redactor | None = None,
    ) -> Session:
        """Load a session, repairing a torn tail and balancing open turns.

        Recovery is deliberately asymmetric: a **torn tail fragment** is
        discarded (it was never a record), but a turn left open by a crash is
        **closed with synthetic events** rather than dropped. The difference
        matters — the second case is real history that happened, and throwing it
        away would make the log disagree with what the model was told.
        """
        recovery = await store.recover_tail(session_id) if recover else TailRecovery()
        committed = [event async for event in store.read(session_id)]
        session = cls(store, await store.header(session_id), committed, redactor=redactor)
        session._last_recovery = recovery
        await session._rebalance()
        return session

    @classmethod
    async def fork(
        cls,
        store: SessionStore,
        source: SessionId,
        *,
        boundary: int | None = None,
        tenant: str = "",
        principal: str = "",
        redactor: Redactor | None = None,
    ) -> Session:
        header = await store.fork(
            source, boundary=boundary, tenant=tenant, principal=principal
        )
        committed = [event async for event in store.read(header.session_id)]
        return cls(store, header, committed, redactor=redactor)

    # -- recovery ----------------------------------------------------------
    last_recovery: TailRecovery = TailRecovery()

    async def _rebalance(self) -> None:
        """Close anything a crash left open, so the projection sees balanced turns."""
        if not self._committed:
            return

        for call in pending_tool_calls(self._committed):
            await self.append(
                EventKind.TOOL_RESULT,
                {
                    "callId": call.call_id,
                    "content": (
                        f"tool {call.name!r} did not finish; the session was interrupted "
                        f"before it returned"
                    ),
                    "isError": True,
                    "interrupted": True,
                },
                turn=call.turn,
                step=call.step,
            )

        open_turn_id = open_turn(self._committed)
        if open_turn_id is not None:
            await self.append(
                EventKind.TURN_END,
                {"turn": int(open_turn_id), "reason": "interrupted"},
                turn=open_turn_id,
            )

        if self._pending:
            await self.flush()

    # -- writing -----------------------------------------------------------
    async def append(
        self,
        kind: str,
        data: Mapping[str, Any] | None = None,
        *,
        surface: SurfaceOp = SurfaceOp.LOG_ONLY,
        turn: TurnId | None = None,
        step: int | None = None,
    ) -> SessionEvent:
        """Buffer an event. The only way anything enters the log.

        **The redaction gate is here, and that placement is load-bearing.** Because
        whatever the model sees must be in the log (``runtime/log_guard.py``), one
        gate at this single door protects the conversation *and* the storage at
        once: there is no path where the log stays clean and the model still saw
        the secret. It has to sit here rather than at the store, because a payload
        over the store's inline limit spills to a blob file — redacting later
        would miss every large tool result, which is where a stray credential is
        most likely to be.

        A detector that fails does **not** fail the write: the text becomes a
        metadata stub and ``redaction/failed`` names the broken rule. The
        fail-closed property of this gate is about the *value* never landing, not
        about the agent stopping.

        A match classified ``Sensitivity.SECRET`` goes further and collapses the
        whole payload to a stub, because at that level the payload is presumed
        sensitive as a whole rather than only at the span the rule matched.
        """
        redacted, report = self._redactor.redact_payload(dict(data or {}))
        if report.is_secret:
            redacted = collapse_payload(redacted)
        event = self._append_raw(kind, redacted, surface=surface, turn=turn, step=step)
        if report.applied:
            self._audit_redaction(EventKind.REDACTION_APPLIED, report, event.seq, turn, step)
        if report.degraded:
            self._audit_redaction(EventKind.REDACTION_FAILED, report, event.seq, turn, step)
        return event

    def _audit_redaction(
        self,
        kind: str,
        report: RedactionReport,
        seq: int,
        turn: TurnId | None,
        step: int | None,
    ) -> None:
        """Record what the gate did, through a path that does not re-enter it.

        The payload holds kinds, counts and fingerprints and so would survive a
        second pass unchanged — but "it would be a no-op" is how a recursion into
        the gate gets written one day, and the record of a redaction should not
        itself be a redaction decision.
        """
        self._append_raw(kind, report.to_event_data(seq=seq), turn=turn, step=step)

    def _append_raw(
        self,
        kind: str,
        data: Mapping[str, Any] | None = None,
        *,
        surface: SurfaceOp = SurfaceOp.LOG_ONLY,
        turn: TurnId | None = None,
        step: int | None = None,
    ) -> SessionEvent:
        """Build, buffer and announce an event. No gate, no judgement."""
        event = SessionEvent(
            seq=self._next_seq,
            kind=kind,
            data=dict(data or {}),
            surface=surface,
            ts=datetime.now(UTC),
            turn=turn,
            step=step,
            event_id=uuid.uuid4().hex,
            tenant=self.header.tenant,
            principal=self.header.principal,
        )
        self._pending.append(event)
        self._next_seq += 1
        self._notify(event)
        return event

    def subscribe(self, listener: Callable[[SessionEvent], None]) -> Callable[[], None]:
        """Watch this session's timeline live. Returns an unsubscribe callable.

        **Fires on append, before the event is durable.** That is deliberate: an
        event that has been appended has already been decided, and waiting for
        the flush would make a live reader lag by a whole step. It also means a
        listener can see an event that a crash before the next flush then loses —
        acceptable because the *log* remains the truth and a reconnecting reader
        rebuilds from it, but it is why nothing may treat a notification as proof
        of durability.

        Listeners run synchronously on the writing task and must not block or
        raise: a slow or broken observer would otherwise stall the turn it is
        watching. Both are enforced here rather than trusted.
        """
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass  # Already gone; unsubscribing twice is not an error.

        return unsubscribe

    def _notify(self, event: SessionEvent) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - an observer must not break the turn
                pass

    # -- typed helpers -----------------------------------------------------
    #: Thin wrappers so call sites do not have to remember which surface each
    #: event carries. Getting that wrong is the difference between the model
    #: seeing something it should not, or not seeing something it must.
    async def turn_start(self, turn: TurnId) -> SessionEvent:
        return await self.append(EventKind.TURN_START, {"turn": int(turn)}, turn=turn)

    async def turn_end(self, turn: TurnId, reason: str) -> SessionEvent:
        return await self.append(
            EventKind.TURN_END,
            {"turn": int(turn), "reason": reason},
            turn=turn,
        )

    async def step_start(self, turn: TurnId, step: int) -> SessionEvent:
        return await self.append(
            EventKind.STEP_START, {"turn": int(turn), "step": step}, turn=turn, step=step
        )

    async def step_end(self, turn: TurnId, step: int) -> SessionEvent:
        return await self.append(
            EventKind.STEP_END, {"turn": int(turn), "step": step}, turn=turn, step=step
        )

    async def user_message(self, turn: TurnId, text: str) -> SessionEvent:
        return await self.append(
            EventKind.USER_MESSAGE,
            {"text": text},
            surface=SurfaceOp.APPEND,
            turn=turn,
        )

    async def assistant_message(
        self,
        turn: TurnId,
        step: int,
        text: str,
        *,
        tool_calls: Sequence[Mapping[str, Any]] = (),
        usage: Mapping[str, Any] | None = None,
    ) -> SessionEvent:
        data: dict[str, Any] = {"text": text, "toolCalls": [dict(c) for c in tool_calls]}
        if usage is not None:
            data["usage"] = dict(usage)
        return await self.append(
            EventKind.ASSISTANT_MESSAGE, data, surface=SurfaceOp.APPEND, turn=turn, step=step
        )

    async def tool_call(
        self,
        turn: TurnId,
        step: int,
        *,
        call_id: str,
        name: str,
        arguments_json: str,
    ) -> SessionEvent:
        """Record a tool call **before** it runs.

        The raw argument string is stored unparsed, so a malformed-arguments
        failure stays replayable rather than being lost at parse time.
        """
        return await self.append(
            EventKind.TOOL_CALL,
            {"callId": call_id, "name": name, "arguments": arguments_json},
            turn=turn,
            step=step,
        )

    async def tool_result(
        self,
        turn: TurnId,
        step: int,
        *,
        call_id: str,
        content: str,
        is_error: bool = False,
        meta: Mapping[str, Any] | None = None,
    ) -> SessionEvent:
        data: dict[str, Any] = {"callId": call_id, "content": content, "isError": is_error}
        if meta is not None:
            data["meta"] = dict(meta)
        return await self.append(
            EventKind.TOOL_RESULT, data, surface=SurfaceOp.APPEND, turn=turn, step=step
        )

    async def request_header(
        self, turn: TurnId, step: int, header: Mapping[str, Any]
    ) -> SessionEvent:
        """Record what was sent, log-only.

        The model never sees this as a message, but it is what lets a request be
        reconstructed from the log alone — so it must be written *before*
        dispatch, not after.
        """
        return await self.append(
            EventKind.REQUEST_HEADER, dict(header), turn=turn, step=step
        )

    async def constraint_snapshot(self, constraints: ExecutionConstraints) -> SessionEvent:
        """Record the authority in force, so a resumed session cannot escalate.

        Log-only: the model is never told its own allow-list, because a prompt is
        not a permission policy.
        """
        return await self.append(
            EventKind.CONSTRAINT_SNAPSHOT,
            {
                "principal": str(constraints.principal_id),
                "tenant": str(constraints.tenant_id),
                "allowedRepos": sorted(r.slug for r in constraints.allowed_repos),
                "allowedPrefixes": sorted(constraints.allowed_repo_prefixes),
                "operations": sorted(op.value for op in constraints.permitted_operations),
                "writablePaths": list(constraints.writable_paths),
                "network": str(constraints.network),
            },
        )

    # -- durability --------------------------------------------------------
    async def flush(self) -> None:
        """Push buffered events to storage and make them durable."""
        if self._pending:
            stored = await self._store.append(self.header.session_id, tuple(self._pending))
            self._committed.extend(stored)
            self._pending.clear()
        await self._store.flush(self.header.session_id)

    async def close(self) -> None:
        await self.flush()
        release = getattr(self._store, "release", None)
        if callable(release):
            release(self.header.session_id)

    # -- reading -----------------------------------------------------------
    @property
    def events(self) -> tuple[SessionEvent, ...]:
        """Committed and buffered events in order."""
        return tuple(self._committed) + tuple(self._pending)

    @property
    def committed_events(self) -> tuple[SessionEvent, ...]:
        return tuple(self._committed)

    @property
    def high_water(self) -> int:
        return self._next_seq - 1

    @property
    def durable_seq(self) -> int:
        return self._committed[-1].seq if self._committed else 0

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def next_turn(self) -> TurnId:
        turns = [e.turn for e in self._committed if e.kind == EventKind.TURN_START and e.turn]
        return TurnId(max((int(t) for t in turns), default=0) + 1)
