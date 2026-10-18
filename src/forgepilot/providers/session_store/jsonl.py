"""Append-only JSONL session store — service ``sessionPersistence``.

Layout::

    <root>/<tenant>/<yyyymm>/
        <session_id>.header.json     written once; locates the session
        <session_id>.jsonl           the event stream, one JSON object per line
        <session_id>.lock            single-writer lease
        blobs/<session_id>/<event>.json

Decisions worth knowing, and why:

**Uncompressed and line-oriented.** DSH ships ``.jsonl.zstd``; we deliberately do
not compress. When an agent run goes wrong, being able to ``grep`` and ``jq`` the
timeline is worth more than the disk, and compression is a one-line change inside
this provider later.

**Binary append with an explicit ``\\n``.** Opening in text mode on Windows
translates ``\\n`` to ``\\r\\n``, which corrupts the record framing and only shows
up when the same log is read on Linux. Every write here is bytes.

**Large payloads spill to blobs losslessly.** A tool result can be megabytes; a
log of inline megabytes is unusable. Spilling does **not** truncate: ``read``
rehydrates the blob, so replay still reconstructs exactly what was written. That
matters because the alternative — silently keeping a shortened copy — would break
*model-visible means logged* the moment anyone replayed the session.

**Sequence numbers are validated, not assigned.** The caller owns them (a
buffered session needs them before flushing); this store rejects a non-contiguous
batch rather than quietly renumbering it, so a bug surfaces instead of hiding.

**Single writer per session.** Appends are only atomic for small records on
POSIX, and not guaranteed at all on Windows, so append-only correctness rests on
there being one writer. The lease makes that enforced rather than assumed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.kernel.errors import ForgePilotError
from forgepilot.runtime.events import open_turn, pending_tool_calls
from forgepilot.seams.refs import SessionId
from forgepilot.seams.session import (
    SERVICE_NAME,
    CorruptLog,
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

__all__ = [
    "JsonlSessionStorePlugin",
    "SessionLocked",
]

DEFAULT_INLINE_LIMIT = 64 * 1024
_TENANT_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class SessionLocked(ForgePilotError):
    """Another writer holds the session lease.

    Provider-specific rather than part of the seam: an in-memory store has no
    lease to contend for.
    """


def _safe(component: str, *, fallback: str) -> str:
    cleaned = _TENANT_SAFE.sub("_", component).strip("._-")
    return cleaned or fallback


class JsonlSessionStorePlugin(PluginService):
    """Serves ``sessionPersistence`` over a directory of JSONL files."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        root = cfg.get("root")
        if root:
            self._root = Path(root).expanduser()
        else:
            home = os.environ.get("FORGEPILOT_HOME")
            self._root = (Path(home) if home else Path.home() / ".forgepilot") / "sessions"
        self._root.mkdir(parents=True, exist_ok=True)

        self.inline_limit: int = int(cfg.get("inline_limit", DEFAULT_INLINE_LIMIT))
        self._handles: dict[SessionId, Any] = {}
        self._high_water: dict[SessionId, int] = {}
        self._locations: dict[SessionId, Path] = {}
        self._locks: dict[SessionId, _SessionLock] = {}

    # -- paths -------------------------------------------------------------
    def _shard(self, tenant: str, when: datetime) -> Path:
        return self._root / _safe(tenant, fallback="_default") / when.strftime("%Y%m")

    def _paths(self, sid: SessionId) -> tuple[Path, Path]:
        directory = self._locate(sid)
        return directory / f"{sid}.jsonl", directory / f"{sid}.header.json"

    def _locate(self, sid: SessionId) -> Path:
        cached = self._locations.get(sid)
        if cached is not None:
            return cached
        # A real deployment indexes this; globbing is correct but linear in the
        # number of sessions, which is a known and accepted skeleton limitation.
        matches = list(self._root.glob(f"*/*/{sid}.header.json"))
        if not matches:
            raise FileNotFoundError(f"no session {sid!r} under {self._root}")
        directory = matches[0].parent
        self._locations[sid] = directory
        return directory

    # -- lease -------------------------------------------------------------
    def _acquire(self, sid: SessionId, jsonl: Path) -> None:
        if sid in self._locks:
            return
        lock = _SessionLock(jsonl.with_suffix(".lock"))
        lock.acquire()
        self._locks[sid] = lock

    def release(self, session_id: SessionId) -> None:
        """Drop the writer lease and close the append handle."""
        handle = self._handles.pop(session_id, None)
        if handle is not None:
            handle.close()
        lock = self._locks.pop(session_id, None)
        if lock is not None:
            lock.release()

    # -- SessionStore ------------------------------------------------------
    async def create(self, header: SessionHeader) -> None:
        directory = self._shard(header.tenant, header.created_at)
        directory.mkdir(parents=True, exist_ok=True)
        jsonl = directory / f"{header.session_id}.jsonl"
        header_path = directory / f"{header.session_id}.header.json"

        if not header_path.exists():
            _atomic_write_json(
                header_path,
                {
                    "session_id": str(header.session_id),
                    "created_at": header.created_at.isoformat(),
                    "format_version": header.format_version,
                    "tenant": header.tenant,
                    "principal": header.principal,
                    "cwd": header.cwd,
                    "parent_session": str(header.parent_session)
                    if header.parent_session
                    else None,
                    "seed_boundary": header.seed_boundary,
                },
            )
        jsonl.touch(exist_ok=True)

        self._locations[header.session_id] = directory
        self._high_water.setdefault(header.session_id, 0)
        self._acquire(header.session_id, jsonl)

    async def header(self, session_id: SessionId) -> SessionHeader:
        _, header_path = self._paths(session_id)
        raw = json.loads(header_path.read_text(encoding="utf-8"))
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

    async def append(
        self, session_id: SessionId, events: Sequence[SessionEvent]
    ) -> Sequence[SessionEvent]:
        if not events:
            return ()
        jsonl, _ = self._paths(session_id)
        expected = self._water(session_id)

        for offset, event in enumerate(events, start=1):
            if event.seq != expected + offset:
                raise NonContiguousSequence(
                    f"event {event.seq} does not continue the log at {expected + offset}; "
                    f"the log has {expected} events. Sequence numbers must be contiguous "
                    f"and are never renumbered for you."
                )

        # Validate the whole batch before writing anything. A batch that fails
        # half way would leave orphan blobs behind and, worse, would have spent
        # sequence numbers on events that were never persisted.
        for event in events:
            _assert_storable(event)

        stored: list[SessionEvent] = []
        payload = bytearray()
        for event in events:
            materialised = event if event.event_id else replace(event, event_id=uuid.uuid4().hex)
            payload += _encode(materialised, jsonl.parent, self.inline_limit).encode("utf-8")
            payload += b"\n"
            stored.append(materialised)

        handle = self._handles.get(session_id)
        if handle is None:
            self._acquire(session_id, jsonl)
            handle = open(jsonl, "ab")  # noqa: SIM115 - held open for fsync semantics
            self._handles[session_id] = handle
        handle.write(bytes(payload))
        handle.flush()

        self._high_water[session_id] = expected + len(stored)
        return tuple(stored)

    async def read(self, session_id: SessionId, *, after: int = 0) -> AsyncIterator[SessionEvent]:
        jsonl, _ = self._paths(session_id)
        if not jsonl.exists():
            return
        for event in _parse_lines(jsonl, jsonl.parent):
            if event.seq > after:
                yield event

    async def high_water(self, session_id: SessionId) -> int:
        return self._water(session_id)

    async def flush(self, session_id: SessionId) -> None:
        handle = self._handles.get(session_id)
        if handle is None:
            return
        handle.flush()
        os.fsync(handle.fileno())

    async def recover_tail(self, session_id: SessionId) -> TailRecovery:
        jsonl, _ = self._paths(session_id)
        if not jsonl.exists():
            return TailRecovery(last_valid_seq=0, detail="no log yet")

        data = jsonl.read_bytes()
        if not data:
            return TailRecovery(last_valid_seq=0, detail="empty log")

        parts = data.split(b"\n")
        tail = parts.pop()  # empty when the file ends with a newline

        dropped = False
        if tail:
            # A fragment with no terminating newline was never fully written.
            dropped = True
        elif parts and not _is_json_line(parts[-1]):
            # Complete-looking but unparsable: torn by a crash mid-write.
            parts.pop()
            dropped = True

        if dropped:
            jsonl.write_bytes(b"".join(line + b"\n" for line in parts))
            self._high_water.pop(session_id, None)

        last_seq = 0
        if parts and _is_json_line(parts[-1]):
            last_seq = int(json.loads(parts[-1])["seq"])

        return TailRecovery(
            truncated_partial_tail=dropped,
            last_valid_seq=last_seq,
            detail="discarded a torn tail fragment" if dropped else "log ends cleanly",
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

        # Refuse rather than clip: a caller who asked for a boundary inside an
        # open turn wants a history we cannot give them, and quietly returning a
        # tidier one would be a different history than they asked for.
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
        # Mark where the seed ends. Seed history and live work are otherwise
        # byte-identical, which would leave any plugin owning an open/close
        # bracket unable to tell "replaying" from "running".
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
        await self.flush(child)
        return header

    async def summaries(self, *, limit: int = 50, cursor: str | None = None) -> Page:
        """Session headers, newest first — the order the seam promises.

        Sorting costs a read of every header, because the store keeps no time index.
        The alternative is to paginate the directory in path order, which within one
        tenant *is* chronological (the session id carries its timestamp) but across
        tenants groups by name — so the first page could be the oldest sessions of the
        alphabetically-first tenant. A listing that cannot answer "what just happened"
        is the reason nothing called this for two eras.
        """
        found: list[tuple[str, dict[str, Any]]] = []
        for path in self._root.glob("*/*/*.header.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            found.append((str(raw.get("created_at") or ""), raw))
        found.sort(key=lambda pair: pair[0], reverse=True)
        start = int(cursor) if cursor else 0
        window = found[start : start + limit]
        items = []
        for _created, raw in window:
            sid = SessionId(raw["session_id"])
            created = raw.get("created_at")
            items.append(
                SessionSummary(
                    session_id=sid,
                    created_at=datetime.fromisoformat(created) if created else None,
                    event_count=await self.high_water(sid),
                    tenant=str(raw.get("tenant", "")),
                    principal=str(raw.get("principal", "")),
                )
            )
        next_cursor = str(start + limit) if start + limit < len(found) else None
        return Page(items=tuple(items), next_cursor=next_cursor)

    async def delete(self, session_id: SessionId) -> None:
        self.release(session_id)
        jsonl, header_path = self._paths(session_id)
        blobs = jsonl.parent / "blobs" / str(session_id)
        jsonl.unlink(missing_ok=True)
        header_path.unlink(missing_ok=True)
        if blobs.exists():
            for child in blobs.iterdir():
                child.unlink(missing_ok=True)
            blobs.rmdir()
        self._high_water.pop(session_id, None)
        self._locations.pop(session_id, None)

    # -- internals ---------------------------------------------------------
    def _water(self, session_id: SessionId) -> int:
        cached = self._high_water.get(session_id)
        if cached is not None:
            return cached
        jsonl, _ = self._paths(session_id)
        last = 0
        if jsonl.exists():
            for event in _parse_lines(jsonl, jsonl.parent):
                last = event.seq
        self._high_water[session_id] = last
        return last


# ---------------------------------------------------------------------------
# Encoding / decoding
# ---------------------------------------------------------------------------
def _assert_storable(event: SessionEvent) -> None:
    """Fail loudly on a payload storage cannot round-trip.

    Without this the raw ``TypeError`` from ``json.dumps`` escapes, which is both
    a worse error to read and a breach of the store contract: a caller cannot
    write code that handles "this payload is unstorable" if each implementation
    reports it differently.
    """
    try:
        json.dumps(event.to_json(), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise LossyEventPayload(
            f"event {event.kind!r} carries a payload that cannot be stored losslessly, "
            f"so 'model-visible means logged' would already be broken: {exc}"
        ) from exc


def _encode(event: SessionEvent, directory: Path, inline_limit: int) -> str:
    """Serialise an event, spilling an oversized payload to a blob."""
    rendered = json.dumps(event.to_json(), ensure_ascii=False)
    if len(rendered.encode("utf-8")) <= inline_limit:
        return rendered

    raw = json.dumps(dict(event.data), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    blob_dir = directory / "blobs" / str(event.tenant or "_default")
    blob_dir.mkdir(parents=True, exist_ok=True)
    name = f"{event.event_id or event.seq}.json"
    (blob_dir / name).write_bytes(raw)

    materialised = event.to_json()
    materialised["data"] = {}
    materialised["blob"] = {
        "path": f"blobs/{event.tenant or '_default'}/{name}",
        "bytes": len(raw),
        "sha256": digest,
    }
    return json.dumps(materialised, ensure_ascii=False)


def _is_json_line(line: bytes) -> bool:
    try:
        json.loads(line)
    except Exception:  # noqa: BLE001 - any parse failure means "not a record"
        return False
    return True


def _parse_lines(path: Path, directory: Path) -> list[SessionEvent]:
    """Parse a log, rehydrating spilled blobs and refusing mid-file corruption."""
    events: list[SessionEvent] = []
    for index, line in enumerate(path.read_bytes().split(b"\n")):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CorruptLog(
                f"{path.name} line {index + 1} is not valid JSON ({exc}). Corruption away "
                f"from the tail is not repaired: a log with a hole in the middle is not "
                f"a log, and excising it would silently change history. Run "
                f"recover_tail() first if the damage is at the end."
            ) from exc
        events.append(SessionEvent.from_json(_rehydrate(raw, directory)))
    return events


def _rehydrate(raw: dict[str, Any], directory: Path) -> dict[str, Any]:
    blob = raw.pop("blob", None)
    if not blob:
        return raw
    blob_path = directory / str(blob["path"])
    try:
        payload = blob_path.read_bytes()
    except FileNotFoundError as exc:
        raise CorruptLog(
            f"event {raw.get('seq')} references blob {blob_path.name} which is missing. "
            f"The log cannot be replayed without it."
        ) from exc
    digest = hashlib.sha256(payload).hexdigest()
    if digest != blob.get("sha256"):
        raise CorruptLog(
            f"blob {blob_path.name} does not match its recorded digest; the payload was "
            f"altered or truncated."
        )
    raw["data"] = json.loads(payload)
    return raw


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Single-writer lease
# ---------------------------------------------------------------------------
#: Paths held by *this* process. An OS advisory lock alone would not reliably
#: refuse a second acquisition from the same process on every platform, and the
#: most likely double-writer in practice is one process starting two runs of the
#: same session.
_HELD_BY_PROCESS: set[str] = set()


class _SessionLock:
    """An exclusive lease on one session's log.

    Combines a process-local registry with an OS advisory lock: the registry
    catches same-process contention deterministically, the OS lock catches other
    processes and is released automatically if this one dies (which is why a
    pid-file scheme is not used — it cannot tell a live holder from a stale one).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def acquire(self) -> None:
        key = str(self.path.resolve())
        if key in _HELD_BY_PROCESS:
            raise SessionLocked(
                f"session log {self.path.name} already has a writer in this process. "
                f"One session has one writer; a second would interleave records."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - held for the lease's lifetime
        try:
            _os_lock(handle)
        except OSError as exc:
            handle.close()
            raise SessionLocked(
                f"another process holds the lease on {self.path.name}: {exc}"
            ) from exc
        self._handle = handle
        _HELD_BY_PROCESS.add(key)

    def release(self) -> None:
        key = str(self.path.resolve())
        _HELD_BY_PROCESS.discard(key)
        if self._handle is None:
            return
        try:
            _os_unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None


def _os_lock(handle: Any) -> None:
    if os.name == "nt":  # pragma: no cover - platform specific
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - platform specific
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(handle: Any) -> None:
    if os.name == "nt":  # pragma: no cover - platform specific
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:  # pragma: no cover - platform specific
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
