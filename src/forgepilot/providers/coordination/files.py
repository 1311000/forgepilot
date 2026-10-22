"""A coordinator that survives a process: files, exclusive creates, and polling.

Two mechanisms, and each is doing exactly one job:

- a **lease** is a JSON file per session, rewritten atomically (temp file +
  :func:`os.replace`). Expiry is a wall-clock field, not a lock, so a replica
  that dies leaves nothing held past ``ttl_s``;
- an **answer** is a file created with ``"x"``. The exclusive create is the
  one-shot property: the second answer to the same question fails with
  ``FileExistsError``, on any replica, without coordination.

The pending record is written once and never rewritten, so a late duplicate POST
cannot change a question that has already been answered — the answer file is
what decides, and it can only be created once.

What this is not: a database. It assumes a POSIX-ish or Windows local filesystem
with atomic ``rename`` in a directory, which is what a state directory is; a
network filesystem is called out in ``docs/notes/deployment.md`` as unsupported
for the same reason the memory store says so.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from forgepilot.kernel import PluginService
from forgepilot.seams.approval import ApprovalOutcome, ApprovalRequest
from forgepilot.seams.coordination import (
    CoordinationUnavailable,
    Lease,
)
from forgepilot.seams.refs import SessionId

#: Reserved lease keys. A lease is keyed by an id, and these two are not sessions but
#: the two roles that must exclude each other: "a process is writing" (many holders,
#: one file each) and "a backup is running" (one holder).
WRITER_PREFIX = "writer:"
MAINTENANCE = SessionId("deployment:maintenance")

__all__ = ["FileCoordination", "FileCoordinationPlugin"]

#: How often a waiter looks for an answer. 100 ms is under the latency a human
#: notices, and the alternative (a filesystem watch) does not exist portably.
DEFAULT_POLL_S = 0.1


def _key(value: str) -> str:
    """A filename for an id that may contain anything."""
    return quote(str(value), safe="")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:  # pragma: no cover - permissions are the operator's problem
        raise CoordinationUnavailable(f"cannot read {path}: {exc}") from exc
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CoordinationUnavailable(f"{path} is not JSON: {exc}") from exc
    return loaded if isinstance(loaded, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write atomically: a half-written lease is a lease nobody can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


@dataclass
class FileCoordination:
    """The mechanics, without a kernel — so tests can use two of them."""

    root: Path
    poll_s: float = DEFAULT_POLL_S
    clock: Callable[[], float] = time.time

    def _leases(self) -> Path:
        return self.root / "leases"

    def _approvals(self) -> Path:
        return self.root / "approvals"

    def _lease_path(self, session_id: SessionId) -> Path:
        return self._leases() / f"{_key(session_id)}.json"

    def _pending_path(self, request_id: str) -> Path:
        return self._approvals() / f"{_key(request_id)}.pending.json"

    def _answer_path(self, request_id: str) -> Path:
        return self._approvals() / f"{_key(request_id)}.answer.json"

    # -- leases ------------------------------------------------------------
    async def acquire(self, session_id: SessionId, *, holder: str, ttl_s: float) -> Lease | None:
        now = self.clock()
        path = self._lease_path(session_id)
        current = _read_json(path)
        fencing = 0
        if current is not None:
            fencing = int(current.get("fencing", 0))
            fresh = float(current.get("expires_at", 0.0)) > now
            # A live lease is not stolen, not even by the same holder: a replica
            # that re-acquires while its own turn runs would bump the fencing
            # number under its own feet.
            if fresh:
                return None
            if current.get("holder") == holder:
                # Our own expired lease (a paused process): taking it again is
                # the same owner continuing, not a takeover.
                fencing = max(fencing - 1, 0)
        lease = Lease(
            session_id=session_id, holder=holder, fencing=fencing + 1, expires_at=now + ttl_s
        )
        _write_json(
            path,
            {
                "session_id": str(session_id),
                "holder": holder,
                "fencing": lease.fencing,
                "expires_at": lease.expires_at,
            },
        )
        return lease

    async def renew(self, lease: Lease, *, ttl_s: float) -> Lease | None:
        current = _read_json(self._lease_path(lease.session_id))
        if current is None:
            return None
        if current.get("holder") != lease.holder or int(current.get("fencing", 0)) != lease.fencing:
            return None
        renewed = Lease(
            session_id=lease.session_id,
            holder=lease.holder,
            fencing=lease.fencing,
            expires_at=self.clock() + ttl_s,
        )
        _write_json(
            self._lease_path(lease.session_id),
            {
                "session_id": str(lease.session_id),
                "holder": lease.holder,
                "fencing": renewed.fencing,
                "expires_at": renewed.expires_at,
            },
        )
        return renewed

    async def release(self, lease: Lease) -> None:
        path = self._lease_path(lease.session_id)
        current = _read_json(path)
        if current is None:
            return
        if (
            current.get("holder") == lease.holder
            and int(current.get("fencing", 0)) == lease.fencing
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    async def holder(self, session_id: SessionId) -> str | None:
        current = _read_json(self._lease_path(session_id))
        if current is None:
            return None
        if float(current.get("expires_at", 0.0)) <= self.clock():
            return None
        holder = current.get("holder")
        return str(holder) if holder is not None else None

    # -- writers and the deployment lease ----------------------------------
    async def join_writers(self, *, holder: str, ttl_s: float) -> Lease | None:
        if await self.holder(MAINTENANCE) is not None:
            return None
        return await self.acquire(SessionId(f"{WRITER_PREFIX}{holder}"), holder=holder, ttl_s=ttl_s)

    async def leave_writers(self, lease: Lease) -> None:
        await self.release(lease)

    async def writers(self) -> tuple[str, ...]:
        found: list[str] = []
        for path in sorted(self._leases().glob("*.json")):
            raw = _read_json(path)
            if raw is None:
                continue
            session_id = str(raw.get("session_id", ""))
            if not session_id.startswith(WRITER_PREFIX):
                continue
            if float(raw.get("expires_at", 0.0)) <= self.clock():
                continue
            found.append(str(raw.get("holder", session_id[len(WRITER_PREFIX) :])))
        return tuple(sorted(found))

    async def enter_maintenance(self, *, holder: str, ttl_s: float) -> Lease | None:
        if await self.writers():
            return None
        return await self.acquire(MAINTENANCE, holder=holder, ttl_s=ttl_s)

    async def leave_maintenance(self, lease: Lease) -> None:
        await self.release(lease)

    # -- approvals ---------------------------------------------------------
    async def post_approval(self, request: ApprovalRequest, *, expires_at: float) -> None:
        path = self._pending_path(request.request_id)
        if path.exists():
            return
        _write_json(
            path,
            {
                "request_id": request.request_id,
                "session_id": str(request.session_id),
                "tool_name": request.tool_name,
                "arguments_json": request.arguments_json,
                "reason": request.reason,
                "expires_at": expires_at,
            },
        )

    async def answer_approval(
        self, request_id: str, outcome: ApprovalOutcome, *, principal: str
    ) -> bool:
        path = self._answer_path(request_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # "x" is the one-shot. A second answer is not a second answer, and it
            # does not overwrite the first — that is what makes a replayed POST
            # harmless rather than a second grant.
            with path.open("x", encoding="utf-8") as handle:
                json.dump(
                    {"outcome": str(outcome), "principal": principal, "at": self.clock()},
                    handle,
                    sort_keys=True,
                )
        except FileExistsError:
            return False
        return True

    async def wait_approval(self, request_id: str, *, timeout_s: float) -> ApprovalOutcome | None:
        """Poll for an answer until ``timeout_s``.

        Measured on :func:`time.monotonic`, **not** on :attr:`clock`. A timeout is
        a local question ("has this replica waited long enough"), while a lease
        expiry is a shared one ("do the replicas agree this is dead") — and a
        test that freezes the wall clock to simulate expiry would otherwise make
        this loop spin forever, which is exactly the failure a first run of this
        file produced.
        """
        deadline = time.monotonic() + timeout_s
        path = self._answer_path(request_id)
        while True:
            answer = _read_json(path)
            if answer is not None:
                try:
                    return ApprovalOutcome(str(answer.get("outcome")))
                except ValueError as exc:
                    raise CoordinationUnavailable(
                        f"{path} holds an outcome outside the closed set: {answer.get('outcome')!r}"
                    ) from exc
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self.poll_s)

    async def pending_request(self, request_id: str) -> ApprovalRequest | None:
        for request in await self.pending_approvals_of_any_session():
            if request.request_id == request_id:
                return request
        return None

    async def pending_approvals_of_any_session(self) -> tuple[ApprovalRequest, ...]:
        """Every unanswered question — the by-id lookup has no session to narrow by."""
        found: list[ApprovalRequest] = []
        if not self._approvals().is_dir():
            return ()
        for path in sorted(self._approvals().glob("*.pending.json")):
            payload = _read_json(path)
            if payload is None:
                continue
            request_id = str(payload.get("request_id", ""))
            if self._answer_path(request_id).exists():
                continue
            if float(payload.get("expires_at", 0.0)) <= self.clock():
                continue
            found.append(
                ApprovalRequest(
                    request_id=request_id,
                    session_id=SessionId(str(payload["session_id"])),
                    tool_name=str(payload.get("tool_name", "")),
                    arguments_json=str(payload.get("arguments_json", "")),
                    reason=str(payload.get("reason", "")),
                )
            )
        return tuple(found)

    async def pending_approvals(self, session_id: SessionId) -> tuple[ApprovalRequest, ...]:
        return tuple(
            request
            for request in await self.pending_approvals_of_any_session()
            if request.session_id == session_id
        )


@dataclass
class FileCoordinationPlugin(PluginService):
    """The ``coordination`` row. Config keys: ``state_dir``, ``poll_s``."""

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        raw = dict(config or {})
        home = raw.get("state_dir")
        root = (
            Path(home)
            if home
            else Path(os.environ.get("FORGEPILOT_HOME", "") or Path.home() / ".forgepilot")
        )
        self._files = FileCoordination(root=root, poll_s=float(raw.get("poll_s", DEFAULT_POLL_S)))

    async def acquire(self, session_id: SessionId, *, holder: str, ttl_s: float) -> Lease | None:
        return await self._files.acquire(session_id, holder=holder, ttl_s=ttl_s)

    async def renew(self, lease: Lease, *, ttl_s: float) -> Lease | None:
        return await self._files.renew(lease, ttl_s=ttl_s)

    async def release(self, lease: Lease) -> None:
        await self._files.release(lease)

    async def holder(self, session_id: SessionId) -> str | None:
        return await self._files.holder(session_id)

    async def post_approval(self, request: ApprovalRequest, *, expires_at: float) -> None:
        await self._files.post_approval(request, expires_at=expires_at)

    async def answer_approval(
        self, request_id: str, outcome: ApprovalOutcome, *, principal: str
    ) -> bool:
        return await self._files.answer_approval(request_id, outcome, principal=principal)

    async def wait_approval(self, request_id: str, *, timeout_s: float) -> ApprovalOutcome | None:
        return await self._files.wait_approval(request_id, timeout_s=timeout_s)

    async def pending_approvals(self, session_id: SessionId) -> tuple[ApprovalRequest, ...]:
        return await self._files.pending_approvals(session_id)

    async def pending_request(self, request_id: str) -> ApprovalRequest | None:
        return await self._files.pending_request(request_id)
