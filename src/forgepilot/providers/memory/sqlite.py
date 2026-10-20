"""SQLite-backed memory store — service ``memory``, the durable one.

Why a file at all: a memory that dies with the process is not a memory. Distillation
writes at the end of one session and recall reads at the start of the next, and if
those two moments are not separated by a process exit then "across sessions" is a
claim nothing has ever tested.

**The rules are not in this file.** Audience, tenant, expiry, status, provenance and
the confidence a citation buys live in :mod:`forgepilot.seams.memory` and are shared
verbatim with the in-memory store, so "what may be believed" has one answer and a
second implementation cannot quietly disagree with the first.

What is here is storage: one table, one row per record, and a ``record_json`` column
holding the record itself. The other columns exist to be **indexed and filtered on**
— never to be the source of truth. The row is rebuilt from the JSON on the way out, so
adding a field to :class:`~forgepilot.seams.memory.MemoryRecord` cannot silently drop
it here. The bill for that choice is explicit: a filter that wanted a field which is
not a column reads rows it did not need. Every column that *is* filtered on is
re-checked by :func:`~forgepilot.seams.memory.recall_verdict`, so the SQL narrowing
can only ever drop rows the rule would have skipped anyway.

Read/write: one connection, opened lazily on first use and used from whatever thread
first touched it — SQLite's own thread check is left on, because a store quietly
shared across threads is how a corruption story starts. Writes commit as they happen,
so a process that is killed after a distillation keeps the memories it admitted.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from forgepilot.errors import ForgePilotError
from forgepilot.kernel import PluginService
from forgepilot.seams.memory import (
    COMPETING_STATUSES,
    PRUNABLE_STATUSES,
    RECALLABLE_STATUSES,
    SERVICE_NAME,
    Confidence,
    EventRef,
    MemoryCandidate,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    Provenance,
    ProvenanceKind,
    PruneReport,
    PurgeTombstone,
    RecallQuery,
    RecallVerdict,
    Resolution,
    confirm_record,
    content_hash_of,
    count_recall,
    expire_record,
    may_recall,
    new_provisional_record,
    new_quarantined_record,
    new_recallable_record,
    plan_resolution,
    promote_proposal,
    prunable,
    recall_verdict,
    related_to,
    require_audience,
    require_provisional_provenance,
    require_recallable_provenance,
    require_tenant,
    revoke_record,
    siblings_of,
)
from forgepilot.seams.refs import MemoryId, RepoRef, SessionId, TenantId
from forgepilot.seams.security import ExecutionConstraints

__all__ = [
    "DEFAULT_FILE",
    "SCHEMA",
    "SCHEMA_VERSION",
    "MemorySchemaTooNew",
    "SqliteMemoryStorePlugin",
]

#: Where the file goes when the profile does not say: the same home the session store
#: and the distillation watermark use, so one deployment has one directory to back up.
DEFAULT_FILE = "memory.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id           TEXT    PRIMARY KEY,
    quarantined  INTEGER NOT NULL DEFAULT 0,
    tenant_id    TEXT,
    scope        TEXT    NOT NULL,
    scope_ref    TEXT    NOT NULL,
    type         TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    subject      TEXT    NOT NULL,
    content_hash TEXT    NOT NULL,
    record_json  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_content ON memories (content_hash);
CREATE INDEX IF NOT EXISTS memories_audience
    ON memories (tenant_id, scope, scope_ref, type, status, subject);
CREATE TABLE IF NOT EXISTS purged (
    memory_id    TEXT PRIMARY KEY,
    tenant_id    TEXT,
    subject      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    purged_at    TEXT NOT NULL,
    approved_by  TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT ''
);
"""

#: What this build writes and reads. Stored in SQLite's own ``user_version`` rather
#: than in a metadata table: it is one integer, the file already carries it, and a
#: table that could be missing is a second failure mode.
SCHEMA_VERSION = 1

#: ``version -> SQL`` taking a file from ``version`` to ``version + 1``. Empty at the
#: first version, and that is the point: the machinery exists before it is needed, so
#: the first schema change is a dict entry and a test rather than a redesign. A
#: version 0 file is a *new* file (or one written before this build recorded a
#: version), and ``SCHEMA`` is idempotent, so it is created in place and stamped.
MIGRATIONS: dict[int, str] = {}


class MemorySchemaTooNew(ForgePilotError):
    """The file was written by a build that knows more than this one does.

    Refused rather than read, and the direction matters: a newer file may hold a
    field this build's codec would ignore, and the first write-back would then drop
    it — silently, and only in the deployment that upgraded a binary before its
    store. Downgrading a binary is not supported; this is where it says so.
    """


def schema_version(conn: sqlite3.Connection) -> int:
    """The version recorded in the file. ``0`` is "never stamped"."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the file to :data:`SCHEMA_VERSION`, or refuse it.

    The whole policy is three lines and each one is a decision: *too new* refuses
    (see :class:`MemorySchemaTooNew`), *no version* creates and stamps (idempotent
    DDL, so a pre-version file is adopted without touching its rows), and *behind*
    walks :data:`MIGRATIONS` one step at a time so a file two versions old is
    migrated by the same code as one a single version old.
    """
    version = schema_version(conn)
    if version > SCHEMA_VERSION:
        raise MemorySchemaTooNew(
            f"the memory store at this path records schema version {version}, which is "
            f"newer than this build's {SCHEMA_VERSION}. A newer file may hold fields this "
            f"codec would drop on the next write, and dropping them would look like "
            f"exactly the bug it is. Use the build that wrote the file, or start a new "
            f"store — do not downgrade the binary under a live file."
        )
    if version == SCHEMA_VERSION:
        return
    if version == 0:
        # A brand-new file and a file from before this build recorded a version look
        # identical, and the DDL is idempotent, so both are handled by creating in
        # place. Existing rows are untouched: the columns and the JSON codec are the
        # same ones that wrote them.
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        return
    for step in range(version, SCHEMA_VERSION):
        script = MIGRATIONS.get(step)
        if script is None:
            raise MemorySchemaTooNew(
                f"the memory store is at schema version {version} but this build has no "
                f"migration from version {step}; it knows {sorted(MIGRATIONS)}. Refusing "
                f"rather than reading a file whose shape it cannot promise to preserve."
            )
        conn.executescript(script)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


#: The statuses a record can be recalled *from*. Kept as a tuple, sorted, because the
#: same values are spliced into two SQL statements and a set's iteration order is not
#: something to put in a query.
_LIVE_STATUSES: tuple[str, ...] = tuple(sorted(status.value for status in RECALLABLE_STATUSES))
#: What a resolution compares: an ACTIVE or CONFLICTED record is a belief the system
#: holds. A PROPOSED one is a candidate (`seams/memory.py::COMPETING_STATUSES`).
_COMPETING_STATUSES: tuple[str, ...] = tuple(sorted(status.value for status in COMPETING_STATUSES))
#: The statuses ``prune`` may delete — terminal ones only (`seams/memory.py::PRUNABLE_STATUSES`).
_PRUNABLE_STATUSES: tuple[str, ...] = tuple(sorted(status.value for status in PRUNABLE_STATUSES))

_INSERT = """
INSERT INTO memories
    (id, quarantined, tenant_id, scope, scope_ref, type, status, subject,
     content_hash, record_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    quarantined  = excluded.quarantined,
    tenant_id    = excluded.tenant_id,
    scope        = excluded.scope,
    scope_ref    = excluded.scope_ref,
    type         = excluded.type,
    status       = excluded.status,
    subject      = excluded.subject,
    content_hash = excluded.content_hash,
    record_json  = excluded.record_json
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _moment(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _tenant(value: Any) -> TenantId | None:
    return TenantId(value) if value else None


def _repo_json(repo: RepoRef | None) -> dict[str, str] | None:
    if repo is None:
        return None
    return {"host": repo.host, "namespace": repo.namespace, "name": repo.name}


def _repo(blob: Any) -> RepoRef | None:
    if not blob:
        return None
    return RepoRef(host=blob["host"], namespace=blob["namespace"], name=blob["name"])


def encode_record(record: MemoryRecord) -> str:
    """The whole record as JSON — columns index it, this *is* it.

    ``candidate.metadata`` is whatever a distiller put there, so it is serialised
    with ``default=str``: a value that is not JSON-native is stored as its text
    rather than refusing the write. That is a deliberate narrowness, not a general
    promise — a memory's own fields are all JSON-native.
    """
    candidate = record.candidate
    provenance = record.provenance
    return json.dumps(
        {
            "id": record.id,
            "status": record.status.value,
            "tenant_id": record.tenant_id,
            "subject": record.subject,
            "content_hash": record.content_hash,
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "expires_at": _iso(record.expires_at),
            "supersedes": record.supersedes,
            "superseded_by": record.superseded_by,
            "recall_count": record.recall_count,
            "last_recalled_at": _iso(record.last_recalled_at),
            "confidence": record.confidence.value,
            "candidate": {
                "text": candidate.text,
                "scope": candidate.scope.value,
                "scope_ref": candidate.scope_ref,
                "type": candidate.type.value,
                "subject": candidate.subject,
                "tenant_id": candidate.tenant_id,
                "model_confidence_hint": candidate.model_confidence_hint,
                "redaction_marks": list(candidate.redaction_marks),
                "metadata": dict(candidate.metadata),
            },
            "provenance": {
                "kind": provenance.kind.value,
                "session_id": provenance.session_id,
                "repo": _repo_json(provenance.repo),
                "evidence_refs": [[ref.session_id, ref.seq] for ref in provenance.evidence_refs],
            },
        },
        ensure_ascii=False,
        default=str,
    )


def decode_record(raw: str) -> MemoryRecord:
    """The inverse of :func:`encode_record` — and tolerant of an older row.

    Every optional key is read with ``.get`` and falls back to the dataclass
    default. That is the other half of the migration story: :data:`SCHEMA_VERSION`
    refuses a *newer* file, and this refuses to lose an *older* row when a field is
    added. Without it, adding one field to :class:`MemoryRecord` would turn every
    existing row into a ``KeyError`` on the next recall — a store that keeps working
    right up until the release that reads it.

    Required keys stay required: a row that does not identify itself (no ``id``, no
    ``status``) is not an older row, it is a damaged one, and guessing its shape
    would be the silent interpretation this file exists to avoid.
    """
    blob = json.loads(raw)
    candidate = blob["candidate"]
    provenance = blob["provenance"]
    return MemoryRecord(
        id=MemoryId(blob["id"]),
        candidate=MemoryCandidate(
            text=candidate["text"],
            scope=MemoryScope(candidate["scope"]),
            scope_ref=candidate["scope_ref"],
            type=MemoryType(candidate.get("type", MemoryType.FACT.value)),
            subject=candidate.get("subject", ""),
            tenant_id=_tenant(candidate.get("tenant_id")),
            model_confidence_hint=candidate.get("model_confidence_hint"),
            # One deadline, stored once: admission copies it off the candidate, so
            # the record's is the one recall observes.
            expires_at=_moment(blob.get("expires_at")),
            redaction_marks=tuple(candidate.get("redaction_marks", ())),
            metadata=candidate.get("metadata", {}),
        ),
        provenance=Provenance(
            kind=ProvenanceKind(provenance["kind"]),
            session_id=(
                SessionId(provenance["session_id"]) if provenance.get("session_id") else None
            ),
            repo=_repo(provenance.get("repo")),
            evidence_refs=tuple(
                EventRef(SessionId(session_id), int(seq))
                for session_id, seq in provenance.get("evidence_refs", ())
            ),
        ),
        confidence=Confidence(blob.get("confidence", Confidence.LOW.value)),
        status=MemoryStatus(blob["status"]),
        tenant_id=_tenant(blob.get("tenant_id")),
        subject=blob.get("subject", ""),
        content_hash=blob.get("content_hash", ""),
        created_at=_moment(blob.get("created_at")),
        updated_at=_moment(blob.get("updated_at")),
        expires_at=_moment(blob.get("expires_at")),
        supersedes=MemoryId(blob["supersedes"]) if blob.get("supersedes") else None,
        superseded_by=MemoryId(blob["superseded_by"]) if blob.get("superseded_by") else None,
        recall_count=int(blob.get("recall_count", 0)),
        last_recalled_at=_moment(blob.get("last_recalled_at")),
    )


class SqliteMemoryStorePlugin(PluginService):
    """Serves ``memory`` from a SQLite file."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = config or {}
        raw = cfg.get("path")
        if raw:
            self.path = Path(str(raw)).expanduser()
        else:
            home = os.environ.get("FORGEPILOT_HOME")
            self.path = (Path(home) if home else Path.home() / ".forgepilot") / DEFAULT_FILE

    # -- storage -----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """A fresh connection to the store file. The caller closes it.

        A connection **per operation**, kept in a local, and that is load-bearing
        twice over.

        *The proxy:* a service is reached through Cordis's traceable proxy, which wraps
        a *callable* attribute it reads back — and an open ``sqlite3.Connection`` is
        callable. The first version cached the connection on ``self``; reading it back
        returned a shadow method, and the failure was ``AttributeError: 'function'
        object has no attribute 'execute'`` from three frames away. A local variable is
        never read through the proxy.
        *The file:* a connection released per operation cannot hold a lock that another
        process wants, and on Windows a lingering handle is a temporary directory that
        will not delete.

        Opening is deferred rather than done in ``__init__`` on purpose: the plugin is
        loaded when a profile *resolves*, and a store that created its file there would
        write into a user's home directory during ``--dump-config``, ``--boot`` or
        ``--sandbox-check``. Loading a plugin must not have that side effect; storing a
        memory must.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        # WAL, because a distillation process and a `--chat` process may hold the same
        # file at the same time and the default journal serialises readers against a
        # writer.
        conn.execute("PRAGMA journal_mode=WAL")
        # Create, migrate, or refuse — see `apply_schema`. This replaces the bare
        # `executescript(SCHEMA)` the first version ran on every connection, which
        # made "the file is version 7" a thing nobody could ever find out.
        apply_schema(conn)
        return conn

    def _put(self, record: MemoryRecord, *, quarantined: bool = False) -> None:
        """Write one record — the only write path, so the columns cannot drift."""
        candidate = record.candidate
        with closing(self._connect()) as conn:
            conn.execute(
                _INSERT,
                (
                    record.id,
                    int(quarantined),
                    record.tenant_id,
                    candidate.scope.value,
                    candidate.scope_ref,
                    record.type.value,
                    record.status.value,
                    record.subject,
                    record.content_hash,
                    encode_record(record),
                ),
            )
            conn.commit()

    def _load(self, memory_id: MemoryId) -> MemoryRecord | None:
        """A recallable-store record by id. Quarantined rows are not in it."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT record_json FROM memories WHERE id = ? AND quarantined = 0",
                (memory_id,),
            ).fetchone()
        return decode_record(row[0]) if row is not None else None

    def _live_record(self, digest: str) -> MemoryRecord | None:
        """The recallable record for this content, if a live one exists."""
        marks = ", ".join("?" * len(_LIVE_STATUSES))
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT record_json FROM memories WHERE quarantined = 0 "
                f"AND content_hash = ? AND status IN ({marks}) ORDER BY rowid LIMIT 1",
                (digest, *_LIVE_STATUSES),
            ).fetchone()
        return decode_record(row[0]) if row is not None else None

    def _load_quarantined(self, memory_id: MemoryId) -> MemoryRecord | None:
        """A quarantined record by id — only ``purge`` looks outside the store.

        Quarantined records are never recalled, but they are still content somebody
        stored, so "forget this" has to be able to reach them.
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT record_json FROM memories WHERE id = ? AND quarantined = 1",
                (memory_id,),
            ).fetchone()
        return decode_record(row[0]) if row is not None else None

    def _neighbours(self, record: MemoryRecord) -> tuple[MemoryRecord, ...]:
        """Live records in the same family: tenant, scope, scope_ref and type.

        The SQL narrows on indexed columns only, and the subject comparison — the
        exact conflict key and the fuzzy report — stays in the seam where both stores
        share it. Every row this returns is re-checked by ``siblings_of`` or
        ``related_to`` before anything is decided about it.

        The status filter is :data:`COMPETING_STATUSES`, not the recallable set: a
        ``PROPOSED`` neighbour is a candidate, not a belief, so it must reach neither
        the comparison nor the report.
        """
        marks = ", ".join("?" * len(_COMPETING_STATUSES))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT record_json FROM memories WHERE quarantined = 0 AND tenant_id = ? "
                f"AND scope = ? AND scope_ref = ? AND type = ? AND status IN ({marks}) "
                f"ORDER BY rowid",
                (
                    record.tenant_id,
                    record.candidate.scope.value,
                    record.candidate.scope_ref,
                    record.type.value,
                    *_COMPETING_STATUSES,
                ),
            ).fetchall()
        return tuple(decode_record(raw) for (raw,) in rows)

    @staticmethod
    def _next_id(prefix: str, digest: str) -> MemoryId:
        """An id derived from the claim, not counted per process.

        A counter would restart with every process, so two processes distilling at
        once would both hand out ``mem-1`` and one of them would lose a memory to the
        primary key. The content hash is already the identity this store de-duplicates
        on — deriving the id from it makes the same claim the same row in every
        process, which is what the write path already promises.
        """
        return MemoryId(f"{prefix}-{digest[:16]}")

    # -- MemoryStore -------------------------------------------------------
    async def write(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        require_recallable_provenance(provenance)
        require_tenant(candidate)
        # At admission, not at recall: a scope with no audience model used to be
        # accepted here and then raised on every read, which is a record nobody can
        # ever see, created by a path that said yes.
        require_audience(candidate)
        now = datetime.now(UTC)
        digest = content_hash_of(candidate)

        existing = self._live_record(digest)
        if existing is not None:
            # Same claim: one row. A better provenance promotes the proposal in place
            # rather than being dropped, and the caller is told where the memory is.
            if existing.status is MemoryStatus.PROPOSED:
                self._put(promote_proposal(existing, provenance, now=now))
            return existing.id

        memory_id = self._next_id("mem", digest)
        self._put(new_recallable_record(memory_id, candidate, provenance, now=now))
        return memory_id

    async def admit_provisional(
        self, candidate: MemoryCandidate, provenance: Provenance
    ) -> MemoryId:
        require_provisional_provenance(provenance)
        require_tenant(candidate)
        require_audience(candidate)
        now = datetime.now(UTC)
        digest = content_hash_of(candidate)
        existing = self._live_record(digest)
        if existing is not None:
            return existing.id

        memory_id = self._next_id("prop", digest)
        self._put(new_provisional_record(memory_id, candidate, provenance, now=now))
        return memory_id

    async def quarantine(self, candidate: MemoryCandidate, provenance: Provenance) -> MemoryId:
        digest = content_hash_of(candidate)
        memory_id = self._next_id("quar", digest)
        record = new_quarantined_record(memory_id, candidate, provenance, now=datetime.now(UTC))
        self._put(record, quarantined=True)
        return memory_id

    async def confirm(self, memory_id: MemoryId, evidence: EventRef) -> MemoryRecord:
        record = self._load(memory_id)
        if record is None:
            raise KeyError(f"no memory record {memory_id!r} is admitted")
        confirmed = confirm_record(record, evidence, now=datetime.now(UTC))
        self._put(confirmed)
        return confirmed

    async def recall(
        self, query: RecallQuery, constraints: ExecutionConstraints
    ) -> Sequence[MemoryRecord]:
        if not may_recall(query, constraints):
            return ()
        now = datetime.now(UTC)
        marks = ", ".join("?" * len(_LIVE_STATUSES))
        # Narrowing only: tenant, scope, scope_ref and status are all re-checked by
        # recall_verdict below, which stays the authority. Expiry is deliberately not
        # in this WHERE clause — it is a *verdict*, because observing the deadline is
        # what moves the record to EXPIRED.
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT record_json FROM memories WHERE quarantined = 0 AND tenant_id = ? "
                f"AND scope = ? AND scope_ref = ? AND status IN ({marks}) ORDER BY rowid",
                (constraints.tenant_id, query.scope.value, query.scope_ref, *_LIVE_STATUSES),
            ).fetchall()

        hits: list[MemoryRecord] = []
        for (raw,) in rows:
            record = decode_record(raw)
            verdict = recall_verdict(record, query, constraints, now=now)
            if verdict is RecallVerdict.EXPIRED:
                self._put(expire_record(record, now=now))
                continue
            if verdict is not RecallVerdict.HIT:
                continue
            counted = count_recall(record, now=now)
            self._put(counted)
            hits.append(counted)
            if len(hits) >= query.limit:
                break
        return tuple(hits)

    async def revoke(self, memory_id: MemoryId) -> None:
        record = self._load(memory_id)
        if record is None:
            raise KeyError(f"no memory record {memory_id!r} is admitted")
        self._put(revoke_record(record, now=datetime.now(UTC)))

    async def reconcile(
        self, memory_id: MemoryId, *, now: datetime | None = None
    ) -> tuple[Resolution, ...]:
        """Compare a just-admitted record with the ones already on its subject.

        The decision is :func:`plan_resolution` — shared with the in-memory store, so
        the two cannot resolve the same pair differently; this method supplies the
        neighbours and persists what the decision changed. Only an ``ACTIVE`` record
        is judged: a proposal or a conflict is not the current belief, and an
        admission does not get to settle the subject around it.
        """
        moment = now or datetime.now(UTC)
        record = self._load(memory_id)
        if record is None or record.status is not MemoryStatus.ACTIVE:
            return ()
        neighbours = self._neighbours(record)
        changed, decisions = plan_resolution(record, siblings_of(neighbours, record), now=moment)
        for updated in changed:
            self._put(updated)
        current = self._load(memory_id) or record
        return (*decisions, *related_to(neighbours, current))

    async def purge(
        self,
        memory_id: MemoryId,
        *,
        approved_by: str,
        reason: str = "",
        now: datetime | None = None,
    ) -> PurgeTombstone:
        """Delete the record's row and leave the tombstone that says it existed.

        The row goes rather than being blanked, because the record *is* the row's
        ``record_json``: an ``UPDATE`` that cleared the JSON would leave the columns
        that index it — subject, content hash, tenant — behind, which is most of what
        "forget this" is asking to remove.
        """
        moment = now or datetime.now(UTC)
        record = self._load(memory_id)
        if record is None:
            record = self._load_quarantined(memory_id)
        if record is None:
            raise KeyError(f"no memory record {memory_id!r} is admitted")
        tombstone = PurgeTombstone(
            memory_id=memory_id,
            content_hash=record.content_hash,
            subject=record.subject,
            purged_at=moment,
            approved_by=approved_by,
            tenant_id=record.tenant_id,
            reason=reason,
        )
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.execute(
                "INSERT INTO purged (memory_id, tenant_id, subject, content_hash, purged_at,"
                " approved_by, reason) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(memory_id) DO UPDATE SET purged_at = excluded.purged_at,"
                " approved_by = excluded.approved_by, reason = excluded.reason",
                (
                    str(tombstone.memory_id),
                    tombstone.tenant_id,
                    tombstone.subject,
                    tombstone.content_hash,
                    tombstone.purged_at.isoformat(),
                    tombstone.approved_by,
                    tombstone.reason,
                ),
            )
            conn.commit()
        return tombstone

    async def prune(
        self,
        *,
        now: datetime | None = None,
        older_than_days: int = 90,
        dry_run: bool = False,
    ) -> PruneReport:
        """Delete history rows older than the cutoff — the only aging-out path.

        The age is not a column, so the rows are decoded and :func:`prunable` decides;
        that is the same shape ``_neighbours`` has already taken, and it keeps the
        rule in the seam rather than in SQL. The candidate set is filtered by status
        *in SQL* as well, because the three terminal values are a small fixed list and
        shipping every live row across the boundary to throw it away would be the
        expensive way to read one integer.

        ``dry_run`` answers "what would this delete" without deleting: an operator's
        first run should be able to see the number before it is the number.
        """
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(days=max(0, older_than_days))
        marks = ", ".join("?" * len(_PRUNABLE_STATUSES))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT id, record_json FROM memories WHERE status IN ({marks})",
                (*_PRUNABLE_STATUSES,),
            ).fetchall()
            doomed = [
                (str(memory_id), raw)
                for memory_id, raw in rows
                if prunable(decode_record(raw), cutoff=cutoff)
            ]
            if doomed and not dry_run:
                conn.executemany(
                    "DELETE FROM memories WHERE id = ?", [(memory_id,) for memory_id, _ in doomed]
                )
                conn.commit()
        return PruneReport(
            cutoff=cutoff,
            removed=tuple(MemoryId(memory_id) for memory_id, _ in doomed),
            kept=len(rows) - len(doomed),
            dry_run=dry_run,
        )

    # -- introspection for tests ------------------------------------------
    @property
    def records(self) -> tuple[MemoryRecord, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT record_json FROM memories WHERE quarantined = 0 ORDER BY rowid"
            ).fetchall()
        return tuple(decode_record(raw) for (raw,) in rows)

    @property
    def quarantined(self) -> tuple[MemoryRecord, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT record_json FROM memories WHERE quarantined = 1 ORDER BY rowid"
            ).fetchall()
        return tuple(decode_record(raw) for (raw,) in rows)

    @property
    def tombstones(self) -> tuple[PurgeTombstone, ...]:
        """What purging left: proof that a record existed, and nothing to read."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT memory_id, tenant_id, subject, content_hash, purged_at, approved_by,"
                " reason FROM purged ORDER BY rowid"
            ).fetchall()
        return tuple(
            PurgeTombstone(
                memory_id=MemoryId(row[0]),
                tenant_id=_tenant(row[1]),
                subject=row[2],
                content_hash=row[3],
                purged_at=datetime.fromisoformat(row[4]),
                approved_by=row[5],
                reason=row[6],
            )
            for row in rows
        )
