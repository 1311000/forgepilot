"""Session-storage seam — service name ``sessionPersistence``.

The session log is an **append-only** event stream and the single source of
truth: the transcript, resume, fork, telemetry, the review narrative and memory
distillation all derive from it. Storage is a plugin so the medium can change
(JSONL today, SQLite later) without touching any of that.

Invariant: **model-visible means logged**. Every payload must be losslessly
JSON-serialisable, because an event that storage cannot round-trip has already
broken the invariant at the moment it was written.

Three shapes here come from studying how DeepSeek Harness and Claude Code
actually do it (see ``CLAUDE.md``); each earns its place:

- **Contiguous ``seq``** — the ordering authority. DSH asserts
  ``events[i].seq == i`` over the whole log, including raw chunks.
- **``surface``** — declares whether an event joins the model-visible sequence or
  is durable-but-invisible. Without it, a reader cannot tell which of the mixed
  record types in the log the model ever saw. DSH calls this ``surfaceOp``.
- **``event_id``** — a stable handle, independent of position. Review claims and
  memory provenance cite events; citing a position is fragile, citing an
  identity is not.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from forgepilot.errors import ForgePilotError
from forgepilot.seams.refs import SessionId, TurnId

__all__ = [
    "SERVICE_NAME",
    "FORMAT_VERSION",
    "EventKind",
    "SurfaceOp",
    "TURN_END_REASONS",
    "SessionEvent",
    "SessionHeader",
    "SessionSummary",
    "TailRecovery",
    "Page",
    "SessionStore",
    "NonContiguousSequence",
    "ForkBoundaryInvalid",
    "CorruptLog",
    "LossyEventPayload",
]


# ---------------------------------------------------------------------------
# Contract errors.
#
# These live in the seam rather than in a provider because they describe what
# *any* store must refuse — a contract that only one implementation honours is
# not a contract, and tests written against one provider's private exception
# would be testing that provider rather than the seam.
# ---------------------------------------------------------------------------
class NonContiguousSequence(ForgePilotError):
    """A batch did not continue the log exactly where it left off.

    Raised rather than silently renumbering: a corrected sequence number would
    hide the bug that produced it, and history would have been rewritten for the
    caller without their asking.
    """


class ForkBoundaryInvalid(ForgePilotError):
    """The requested fork boundary is not a valid point to cut at.

    A boundary inside an open turn, or one leaving unanswered tool calls, is
    refused rather than clipped — clipping would hand back a different history
    than the one requested.
    """


class CorruptLog(ForgePilotError):
    """Damage away from the tail, refused rather than repaired."""


class LossyEventPayload(ForgePilotError):
    """An event payload cannot round-trip through storage, so it cannot be logged.

    Storing it would mean the model saw something the log cannot reconstruct,
    which is the state the *model-visible means logged* invariant forbids.
    """

SERVICE_NAME = "sessionPersistence"

#: Bumped when the on-disk event shape changes incompatibly. Stored in the
#: header so a future reader can refuse a log it does not understand rather than
#: misreading it.
FORMAT_VERSION = 1


class EventKind:
    """Event-name vocabulary.

    Not an enum: the set is open, and a plugin may emit its own namespaced
    events. These are the ones the runtime itself defines.
    """

    TURN_START = "turn/start"
    TURN_END = "turn/end"
    STEP_START = "step/start"
    STEP_END = "step/end"
    USER_MESSAGE = "user/message"
    REQUEST_HEADER = "request/header"
    ASSISTANT_MESSAGE = "assistant/message"
    #: A reasoning model's thinking, logged but never replayed. Reasoning is
    #: model *output*, so it is not covered by "model-visible means logged" — but
    #: throwing it away loses most of what a reasoning model did, which matters
    #: for review narrative and for diagnosing a run that went wrong.
    ASSISTANT_REASONING = "assistant/reasoning"
    TOOL_CALL = "tool/call"
    TOOL_RESULT = "tool/result"
    APPROVAL_ASKED = "approval/asked"
    APPROVAL_DECIDED = "approval/decided"
    CONSTRAINT_SNAPSHOT = "constraints/snapshot"
    SESSION_FORKED = "session/forked"
    #: Written immediately after a seeded (resumed or forked) session's history,
    #: marking where the seed ends and live work begins. Log-only, and its
    #: payload is empty — position carries the meaning.
    END_SEED = "session/end-seed"
    TASK_STATE = "task/state"
    #: A check the *harness* ran — the project's build/test commands. Surfaced to
    #: the model, because it needs to see why something failed, but deliberately
    #: **not** a ``tool/result``: the model never asked for it, and a tool result
    #: with no preceding assistant tool call is a malformed conversation that
    #: endpoints reject outright.
    TASK_CHECK = "task/check"
    MEMORY_WRITE = "memory/write"

    # --- redaction --------------------------------------------------------
    # Emitted by the write path, never by a caller: they describe what the gate
    # did to the event they follow. Log-only by construction — the model never
    # needs to be told its input was filtered, and telling it would put the
    # *existence* of a secret into the conversation.
    #: A rule fired. Carries kinds, counts and salted fingerprints, never a value.
    REDACTION_APPLIED = "redaction/applied"
    #: A detector raised, so the text became its degraded stub. The failure is
    #: recorded because a rule that silently stopped matching is exactly the
    #: "field that looks enforced and is not" shape this codebase keeps finding.
    REDACTION_FAILED = "redaction/failed"

    # --- credentials ------------------------------------------------------
    #: A command asked for a credential **by name**, and the arm phase confirmed
    #: the name resolves. **Names only**: the value exists in the argv of a
    #: process, and writing it here would undo the channel in the one place that
    #: outlives the process.
    CREDENTIAL_CLAIMED = "credential/claimed"

    # --- compaction -------------------------------------------------------
    # `COMPACTION_SUMMARY` is the **only** one of these that is model-visible,
    # and it is a `SurfaceOp.REPLACE`: it takes the place of the span it covers.
    # The rest are log-only, which is how a compaction stays auditable — what was
    # covered, what the model was told, and what it cost are all in the log, none
    # of it in the conversation.
    #
    # Opened before anything else and **flushed on its own**, so a crash leaves an
    # unmatched `start` that the next pass can see. That is DSH's log-recorded
    # lock, and it is what stops two compactions running over one session.
    COMPACTION_START = "compaction/start"
    #: One batch of masking: which results were replaced and how much they cost.
    COMPACTION_MASK = "compaction/mask"
    #: The instruction the summariser was given, plus the span and the model — so
    #: the one model call compaction makes is reconstructable from the log, the
    #: same way `request/header` makes the agent's calls reconstructable.
    COMPACTION_REQUEST = "compaction/request"
    COMPACTION_SUMMARY = "compaction/summary"
    COMPACTION_FAILED = "compaction/failed"
    COMPACTION_END = "compaction/end"

    # --- distillation -----------------------------------------------------
    # Same five-step shape as compaction, for the same reason: the one model
    # call distillation makes has to be reconstructable from the log afterwards,
    # and the run has to be resumable *after* a crash rather than repeating a
    # paid call whose result nobody kept. **None of these is model-visible.**
    # Distillation happens between sessions, about a session; the conversation it
    # reads is over, and nothing here belongs in a model's context.
    #
    # `DISTILLATION_REQUEST` records the instruction and the model but not the
    # prompt's message list (that is `derive_messages` of the region, already in
    # the log); `DISTILLATION_SUMMARY` records the model's **raw** output, which
    # is what makes a bad extraction diagnosable after the fact.
    DISTILLATION_START = "distillation/start"
    DISTILLATION_REQUEST = "distillation/request"
    DISTILLATION_SUMMARY = "distillation/summary"
    DISTILLATION_FAILED = "distillation/failed"
    DISTILLATION_END = "distillation/end"


#: Closed set. ``interrupted`` is appended by the recovery sweep for any log that
#: ends inside an open turn, so the projection always sees balanced turns.
TURN_END_REASONS = frozenset(
    {"completed", "rejected", "cancelled", "budget", "error", "interrupted"}
)


class SurfaceOp(StrEnum):
    """How an event relates to the model-visible sequence.

    The default is :attr:`LOG_ONLY`, deliberately. Forgetting to mark an event
    *surface* makes it invisible to the model — a loud, obvious bug. The opposite
    default would silently feed the model something nobody meant to send.
    """

    #: Joins the ordered, model-visible sequence at the end.
    APPEND = "append"
    #: Supersedes an earlier range of the sequence (e.g. a compaction summary).
    REPLACE = "replace"
    #: Durable, never model-visible. Approvals, constraint snapshots, state.
    LOG_ONLY = "log-only"


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """One immutable entry in the log.

    ``seq`` and ``kind`` have no defaults: they are the two things that must
    never be guessed. In particular a defaulted ``seq`` of ``0`` would sit
    outside the contiguous range and be rejected by the store anyway — better to
    be unable to construct it.
    """

    seq: int
    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)
    surface: SurfaceOp = SurfaceOp.LOG_ONLY
    ts: datetime | None = None
    turn: TurnId | None = None
    step: int | None = None
    event_id: str = ""
    tenant: str = ""
    principal: str = ""

    @property
    def is_model_visible(self) -> bool:
        return self.surface is not SurfaceOp.LOG_ONLY

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "seq": self.seq,
            "kind": self.kind,
            "surface": str(self.surface),
            "data": dict(self.data),
        }
        if self.ts is not None:
            payload["ts"] = self.ts.isoformat()
        if self.turn is not None:
            payload["turn"] = int(self.turn)
        if self.step is not None:
            payload["step"] = self.step
        if self.event_id:
            payload["id"] = self.event_id
        if self.tenant:
            payload["tenant"] = self.tenant
        if self.principal:
            payload["principal"] = self.principal
        return payload

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> SessionEvent:
        ts = raw.get("ts")
        turn = raw.get("turn")
        return cls(
            seq=int(raw["seq"]),
            kind=str(raw["kind"]),
            data=dict(raw.get("data") or {}),
            surface=SurfaceOp(str(raw.get("surface", SurfaceOp.LOG_ONLY))),
            ts=datetime.fromisoformat(ts) if isinstance(ts, str) else None,
            turn=TurnId(int(turn)) if turn is not None else None,
            step=int(raw["step"]) if raw.get("step") is not None else None,
            event_id=str(raw.get("id", "")),
            tenant=str(raw.get("tenant", "")),
            principal=str(raw.get("principal", "")),
        )


@dataclass(frozen=True, slots=True)
class SessionHeader:
    """Non-replayable metadata about a log.

    Stored beside the events rather than in them, so locating a session does not
    require reading its whole history — and so that format version and lineage
    survive even if the event stream is truncated.

    ``tenant`` and ``principal`` are first-class (neither DSH nor Claude Code
    carries them). Sessions contain proprietary source code and ticket text, so
    per-tenant retention, encryption and audit are coming; putting them here now
    means those do not require an event-schema migration.
    """

    session_id: SessionId
    created_at: datetime
    format_version: int = FORMAT_VERSION
    tenant: str = ""
    principal: str = ""
    cwd: str = ""
    #: Set when this session was seeded from another (fork, or subagent).
    parent_session: SessionId | None = None
    #: The source seq this session's seed was cut at, inclusive.
    seed_boundary: int | None = None


@dataclass(frozen=True, slots=True)
class TailRecovery:
    """What a crash sweep had to do, if anything."""

    truncated_partial_tail: bool = False
    last_valid_seq: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: SessionId
    created_at: datetime | None = None
    last_event_at: datetime | None = None
    event_count: int = 0
    title: str = ""
    tenant: str = ""


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[Any, ...] = ()
    next_cursor: str | None = None


@runtime_checkable
class SessionStore(Protocol):
    """Durable storage for session logs.

    **Sequence numbers are assigned by the caller and validated here.** The store
    rejects a non-contiguous batch rather than silently renumbering it: a
    silently-corrected seq would mask the bug that produced it. Validation still
    prevents history from being rewritten, which is the property that matters.
    """

    async def create(self, header: SessionHeader) -> None:
        """Create the log and its header. Idempotent for an existing session."""
        ...

    async def header(self, session_id: SessionId) -> SessionHeader: ...

    async def append(
        self, session_id: SessionId, events: Sequence[SessionEvent]
    ) -> Sequence[SessionEvent]:
        """Persist ``events``, returning them as stored (ids filled in)."""
        ...

    def read(self, session_id: SessionId, *, after: int = 0) -> AsyncIterator[SessionEvent]:
        """Read events after ``after``, in seq order, rehydrating any blobs."""
        ...

    async def high_water(self, session_id: SessionId) -> int: ...

    async def flush(self, session_id: SessionId) -> None:
        """Durability barrier. Returns once buffered events are on stable storage."""
        ...

    async def recover_tail(self, session_id: SessionId) -> TailRecovery:
        """Discard a torn tail fragment, returning what was found.

        Only a partially-written tail is discardable. Corruption earlier in the
        file is raised, not repaired — a log with a hole in the middle is not a
        log, and quietly excising it would change history.
        """
        ...

    async def fork(
        self,
        source: SessionId,
        *,
        boundary: int | None = None,
        child_session_id: SessionId | None = None,
        tenant: str = "",
        principal: str = "",
    ) -> SessionHeader:
        """Seed a new session from a prefix of ``source``.

        ``boundary`` is inclusive and defaults to the source's last event. A
        prefix ending inside an open turn is **rejected**, not clipped: silently
        trimming to a tidy boundary would give the caller a different history
        than the one they asked for.
        """
        ...

    async def summaries(self, *, limit: int = 50, cursor: str | None = None) -> Page: ...

    async def delete(self, session_id: SessionId) -> None: ...
