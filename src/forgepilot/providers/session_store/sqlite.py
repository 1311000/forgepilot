"""SQLite session store — service ``sessionPersistence`` for a fleet.

Why a second store exists at all. The JSONL store is one file per session appended by
one process holding an OS lock, and its sequence numbers are chosen by `Session`
before the write. That is a sound single-writer design and it is what the
single-replica deployment runs. It cannot be the *only* design: two replicas that
each compute "last seq + 1" from a value they read a moment ago will write the same
number, and a log with two ``seq = 7`` is not repairable by hand.

So here the **store** is the writer of record:

* ``append`` assigns sequence numbers inside ``BEGIN IMMEDIATE``. A caller may still
  pass them (``Session`` does, because a buffered event needs its seq before it is
  durable) and they are then *validated*; a gap is refused, never renumbered. An
  event whose ``seq`` is ``0`` means "you decide", which is the shape a provider
  that has no buffering can use.
* ``read`` returns rows. SQLite commits or it does not commit, so there is no torn
  tail to trim and ``recover_tail`` says exactly that instead of pretending to look.
* ``fork`` copies rows inside one transaction, so a child can never see half a parent.

What this store deliberately does **not** do: blobs. The JSONL store spills payloads
beside the log because a line has a size that matters (a megabyte-long line is
unreadable by every tool that greps the file). A row has no such problem, so payloads
stay inline and ``inline_limit`` is not a knob here.

WAL is the journal mode because readers must not block the writer: a replica serving
``GET /sessions/{id}`` cannot be made to wait behind another replica's append.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.runtime.events import open_turn, pending_tool_calls
from forgepilot.seams.refs import SessionId
from forgepilot.seams.session import (
    SERVICE_NAME,
    EventKind,
    ForkBoundaryInvalid,
    LossyEventPayload,
    NonContiguousSequence,
    Page,
    SessionEvent,
    SessionHeader,
    SessionSummary,
    TailRecovery,
)

__all__ = ["SqliteSessionStorePlugin"]

DEFAULT_BUSY_TIMEOUT_MS = 5_000

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS headers (
        session_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        payload    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        session_id TEXT    NOT NULL,
        seq        INTEGER NOT NULL,
        payload    TEXT    NOT NULL,
        PRIMARY KEY (session_id, seq)
    )
    """,
)


class SqliteSessionStorePlugin(PluginService):
    """A session store in one SQLite file, safe to read from several processes."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        raw = dict(config or {})
        path = raw.get("path")
        if not path:
            raise ValueError(
                "session_sqlite needs a `path` for its database; there is no default, "
                "because a default inside a package is a database nobody owns"
            )
        self.path = Path(str(path))
        timeout_ms = int(raw.get("busy_timeout_ms", DEFAULT_BUSY_TIMEOUT_MS))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The connection lives in a dict on purpose. A Cordis service instance is
        # handed to consumers through a tracing proxy, and that proxy wraps attribute
        # values it does not recognise (a `sqlite3.Connection` among them) into
        # callables -- so `self._db["conn"]` reads back as a *function* inside methods the
        # proxy invokes. Plain containers pass through untouched, which is why the
        # JSONL store's `self._handles` dict works and this needs the same shape.
        self._db: dict[str, Any] = {}
        self._db["conn"] = sqlite3.connect(
            self.path,
            timeout=timeout_ms / 1000.0,
            isolation_level=None,  # explicit transactions below
            check_same_thread=False,
        )
        connection = self._db["conn"]
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        for statement in _SCHEMA:
            connection.execute(statement)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Close the connection. Owned by this provider, not by the kernel."""
        self._db["conn"].close()

    # -- SessionStore ------------------------------------------------------
    async def create(self, header: SessionHeader) -> None:
        payload = json.dumps(_header_payload(header), ensure_ascii=False)
        self._write(
            "INSERT OR IGNORE INTO headers (session_id, created_at, payload) VALUES (?, ?, ?)",
            (str(header.session_id), header.created_at.isoformat(), payload),
        )

    async def header(self, session_id: SessionId) -> SessionHeader:
        row = (
            self._db["conn"]
            .execute("SELECT payload FROM headers WHERE session_id = ?", (str(session_id),))
            .fetchone()
        )
        if row is None:
            raise FileNotFoundError(f"no session {session_id!r} in {self.path}")
        return _header_from(json.loads(row[0]))

    async def append(
        self, session_id: SessionId, events: Sequence[SessionEvent]
    ) -> Sequence[SessionEvent]:
        if not events:
            return ()
        stored: list[SessionEvent] = []
        self._begin()
        try:
            highest = self._highest_locked(session_id)
            for offset, event in enumerate(events, start=1):
                target = highest + offset
                if event.seq and event.seq != target:
                    raise NonContiguousSequence(
                        f"event {event.seq} does not continue the log at {target}; the log "
                        f"has {highest} events. Sequence numbers must be contiguous and are "
                        f"never renumbered for you."
                    )
                materialised = replace(
                    event,
                    seq=target,
                    event_id=event.event_id or uuid.uuid4().hex,
                )
                try:
                    rendered = json.dumps(materialised.to_json(), ensure_ascii=False)
                except (TypeError, ValueError) as exc:
                    raise LossyEventPayload(
                        f"event {event.kind!r} carries a payload that cannot be stored "
                        f"losslessly, so 'model-visible means logged' would already be "
                        f"broken: {exc}"
                    ) from exc
                self._db["conn"].execute(
                    "INSERT INTO events (session_id, seq, payload) VALUES (?, ?, ?)",
                    (str(session_id), materialised.seq, rendered),
                )
                stored.append(materialised)
            self._commit()
        except BaseException:
            self._db["conn"].execute("ROLLBACK")
            raise
        return tuple(stored)

    async def read(self, session_id: SessionId, *, after: int = 0) -> AsyncIterator[SessionEvent]:
        rows = (
            self._db["conn"]
            .execute(
                "SELECT payload FROM events WHERE session_id = ? AND seq > ? ORDER BY seq",
                (str(session_id), after),
            )
            .fetchall()
        )
        for (payload,) in rows:
            yield SessionEvent.from_json(json.loads(payload))

    async def high_water(self, session_id: SessionId) -> int:
        return self._highest_locked(session_id)

    async def flush(self, session_id: SessionId) -> None:
        """Nothing to do: a committed row is on stable storage.

        Deliberately not a ``wal_checkpoint``. A checkpoint here would make the
        caller wait for a compaction it did not ask for, and WAL checkpoints happen
        on their own schedule; the durability barrier the seam promises is the
        commit, which has already happened.
        """

    async def recover_tail(self, session_id: SessionId) -> TailRecovery:
        last = self._highest_locked(session_id)
        return TailRecovery(
            truncated_partial_tail=False,
            last_valid_seq=last,
            detail="sqlite commits per transaction, so there is no tail fragment to trim",
        )

    async def fork(
        self,
        source: SessionId,
        *,
        boundary: int | None = None,
        child_session_id: SessionId | None = None,
        tenant: str = "",
        principal: str = "",
    ) -> SessionHeader:
        source_header = await self.header(source)
        seed = [event async for event in self.read(source)]
        if boundary is not None:
            seed = [event for event in seed if event.seq <= boundary]
        cut = seed[-1].seq if seed else 0
        if open_turn(seed) is not None or pending_tool_calls(seed):
            raise ForkBoundaryInvalid(
                f"cannot fork session {source!r} at seq {cut}: the prefix ends inside an "
                f"open turn or with unanswered tool calls. Choose a boundary between turns."
            )

        child = child_session_id or SessionId(f"{source}-fork-{uuid.uuid4().hex[:8]}")
        header = SessionHeader(
            session_id=child,
            created_at=datetime.now(UTC),
            tenant=tenant or source_header.tenant,
            principal=principal or source_header.principal,
            cwd=source_header.cwd,
            parent_session=source,
            seed_boundary=cut,
        )
        await self.create(header)
        if seed:
            await self.append(child, [replace(e, tenant=header.tenant) for e in seed])
        await self.append(
            child,
            [
                SessionEvent(
                    seq=cut + 1,
                    kind=EventKind.END_SEED,
                    data={},
                    ts=datetime.now(UTC),
                    tenant=header.tenant,
                    principal=header.principal,
                )
            ],
        )
        return header

    async def summaries(self, *, limit: int = 50, cursor: str | None = None) -> Page:
        rows = (
            self._db["conn"]
            .execute(
                "SELECT session_id, created_at, payload FROM headers ORDER BY created_at DESC, session_id DESC"
            )
            .fetchall()
        )
        start = int(cursor) if cursor else 0
        window = rows[start : start + limit]
        items = []
        for session_id, _created, payload in window:
            raw = json.loads(payload)
            sid = SessionId(session_id)
            items.append(
                SessionSummary(
                    session_id=sid,
                    created_at=datetime.fromisoformat(raw["created_at"])
                    if raw.get("created_at")
                    else None,
                    event_count=await self.high_water(sid),
                    tenant=str(raw.get("tenant", "")),
                    principal=str(raw.get("principal", "")),
                )
            )
        next_cursor = str(start + limit) if start + limit < len(rows) else None
        return Page(items=tuple(items), next_cursor=next_cursor)

    async def delete(self, session_id: SessionId) -> None:
        self._begin()
        try:
            self._db["conn"].execute("DELETE FROM events WHERE session_id = ?", (str(session_id),))
            self._db["conn"].execute("DELETE FROM headers WHERE session_id = ?", (str(session_id),))
            self._commit()
        except BaseException:
            self._db["conn"].execute("ROLLBACK")
            raise

    # -- internals ---------------------------------------------------------
    def _begin(self) -> None:
        self._db["conn"].execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._db["conn"].execute("COMMIT")

    def _highest_locked(self, session_id: SessionId) -> int:
        row = (
            self._db["conn"]
            .execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?",
                (str(session_id),),
            )
            .fetchone()
        )
        return int(row[0])

    def _write(self, statement: str, parameters: tuple[Any, ...]) -> None:
        self._begin()
        try:
            self._db["conn"].execute(statement, parameters)
            self._commit()
        except BaseException:
            self._db["conn"].execute("ROLLBACK")
            raise


def _header_payload(header: SessionHeader) -> dict[str, Any]:
    return {
        "session_id": str(header.session_id),
        "created_at": header.created_at.isoformat(),
        "format_version": header.format_version,
        "tenant": header.tenant,
        "principal": header.principal,
        "cwd": header.cwd,
        "parent_session": str(header.parent_session) if header.parent_session else None,
        "seed_boundary": header.seed_boundary,
    }


def _header_from(raw: dict[str, Any]) -> SessionHeader:
    parent = raw.get("parent_session")
    created = raw.get("created_at")
    return SessionHeader(
        session_id=SessionId(raw["session_id"]),
        created_at=datetime.fromisoformat(created) if created else datetime.now(UTC),
        format_version=int(raw.get("format_version", 1)),
        tenant=str(raw.get("tenant", "")),
        principal=str(raw.get("principal", "")),
        cwd=str(raw.get("cwd", "")),
        parent_session=SessionId(parent) if parent else None,
        seed_boundary=raw.get("seed_boundary"),
    )
