"""Two replicas, one state directory: the lease and the mailbox are shared.

The tests construct two :class:`FileCoordination` objects over the same directory
rather than two kernel plugins, because the point is the *other process's view*,
not the plugin wiring — the wiring is covered by the API-level test in
``tests/httpapi/``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from forgepilot.providers.coordination.files import FileCoordination
from forgepilot.seams.approval import ApprovalOutcome, ApprovalRequest
from forgepilot.seams.coordination import CoordinationUnavailable, Coordinator, Lease
from forgepilot.seams.refs import SessionId

pytestmark = pytest.mark.asyncio

SESSION = SessionId("session-20261022T090000-aaaa1111")


def _replica(root: Path, *, clock=None) -> FileCoordination:
    return FileCoordination(root=root, poll_s=0.01, clock=clock or (lambda: 1000.0))


def _request(request_id: str = "req-1", session_id: SessionId = SESSION) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=request_id,
        session_id=session_id,
        tool_name="run",
        arguments_json='{"command": "git status"}',
    )


async def test_the_seam_is_satisfied(tmp_path: Path) -> None:
    assert isinstance(_replica(tmp_path), Coordinator)


async def test_a_live_lease_is_not_taken_by_a_second_replica(tmp_path: Path) -> None:
    a, b = _replica(tmp_path), _replica(tmp_path)
    lease = await a.acquire(SESSION, holder="replica-a", ttl_s=30.0)
    assert lease is not None
    assert await b.acquire(SESSION, holder="replica-b", ttl_s=30.0) is None
    # A lease, not a name: the seam declares `Lease | None`, and returning a string here is
    # exactly the mismatch that made `registry.owner_of` raise AttributeError in the
    # two-replica test. The fencing number is the part a caller needs and a name cannot carry.
    seen = await b.holder(SESSION)
    assert seen is not None
    assert seen.holder == "replica-a"
    assert seen.fencing == 1


async def test_the_holder_does_not_steal_its_own_live_lease(tmp_path: Path) -> None:
    a = _replica(tmp_path)
    first = await a.acquire(SESSION, holder="replica-a", ttl_s=30.0)
    assert first is not None
    assert await a.acquire(SESSION, holder="replica-a", ttl_s=30.0) is None


async def test_an_expired_lease_is_taken_over_with_a_higher_fencing(tmp_path: Path) -> None:
    now = [1000.0]
    a = _replica(tmp_path, clock=lambda: now[0])
    b = _replica(tmp_path, clock=lambda: now[0])
    first = await a.acquire(SESSION, holder="replica-a", ttl_s=10.0)
    assert first is not None
    now[0] += 11.0
    assert await b.holder(SESSION) is None
    second = await b.acquire(SESSION, holder="replica-b", ttl_s=10.0)
    assert second is not None and second.fencing > first.fencing


async def test_release_hands_the_session_back(tmp_path: Path) -> None:
    a, b = _replica(tmp_path), _replica(tmp_path)
    lease = await a.acquire(SESSION, holder="replica-a", ttl_s=30.0)
    assert lease is not None
    await a.release(lease)
    assert await b.holder(SESSION) is None
    assert await b.acquire(SESSION, holder="replica-b", ttl_s=30.0) is not None


async def test_renew_extends_only_for_the_holder(tmp_path: Path) -> None:
    now = [1000.0]
    a = _replica(tmp_path, clock=lambda: now[0])
    b = _replica(tmp_path, clock=lambda: now[0])
    lease = await a.acquire(SESSION, holder="replica-a", ttl_s=10.0)
    assert lease is not None
    now[0] += 5.0
    renewed = await a.renew(lease, ttl_s=10.0)
    assert renewed is not None and renewed.expires_at == pytest.approx(1015.0)
    stale = Lease(session_id=SESSION, holder="replica-b", fencing=lease.fencing, expires_at=0.0)
    assert await b.renew(stale, ttl_s=10.0) is None


async def test_an_answer_from_another_replica_wakes_the_waiter(tmp_path: Path) -> None:
    a, b = _replica(tmp_path), _replica(tmp_path)
    request = _request()
    await a.post_approval(request, expires_at=2000.0)

    async def waiter():
        return await b.wait_approval(request.request_id, timeout_s=5.0)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert await a.answer_approval(
        request.request_id, ApprovalOutcome.ALLOWED_ONCE, principal="alice"
    )
    assert await asyncio.wait_for(task, timeout=5.0) == ApprovalOutcome.ALLOWED_ONCE


async def test_a_second_answer_is_refused_rather_than_rewriting_the_first(tmp_path: Path) -> None:
    a, b = _replica(tmp_path), _replica(tmp_path)
    request = _request()
    await a.post_approval(request, expires_at=2000.0)
    assert await a.answer_approval(request.request_id, ApprovalOutcome.REJECTED, principal="alice")
    assert not await b.answer_approval(
        request.request_id, ApprovalOutcome.ALLOWED_ONCE, principal="mallory"
    )
    assert await b.wait_approval(request.request_id, timeout_s=0.2) == ApprovalOutcome.REJECTED


async def test_the_waiter_returns_nothing_when_nobody_answers(tmp_path: Path) -> None:
    a = _replica(tmp_path)
    request = _request()
    await a.post_approval(request, expires_at=2000.0)
    assert await a.wait_approval(request.request_id, timeout_s=0.1) is None


async def test_pending_lists_only_this_sessions_unanswered_questions(tmp_path: Path) -> None:
    a, b = _replica(tmp_path), _replica(tmp_path)
    mine, other = _request("req-mine"), _request("req-other", SessionId("session-bbbb"))
    await a.post_approval(mine, expires_at=2000.0)
    await a.post_approval(other, expires_at=2000.0)
    await a.post_approval(_request("req-answered"), expires_at=2000.0)
    await b.answer_approval("req-answered", ApprovalOutcome.REJECTED, principal="alice")

    pending = await b.pending_approvals(SESSION)
    assert [p.request_id for p in pending] == ["req-mine"]
    assert pending[0].tool_name == "run"


async def test_a_corrupt_answer_is_an_error_not_a_verdict(tmp_path: Path) -> None:
    a = _replica(tmp_path)
    request = _request()
    await a.post_approval(request, expires_at=2000.0)
    path = tmp_path / "approvals" / "req-1.answer.json"
    path.write_text('{"outcome": "maybe"}', encoding="utf-8")
    with pytest.raises(CoordinationUnavailable, match="outside the closed set"):
        await a.wait_approval(request.request_id, timeout_s=0.2)


# -- writers, and the deployment lease a backup takes ----------------------------


@pytest.mark.asyncio
async def test_a_maintenance_lease_refuses_a_writer(tmp_path) -> None:
    coordinator = FileCoordination(root=tmp_path)
    maintenance = await coordinator.enter_maintenance(holder="backup", ttl_s=60.0)
    assert maintenance is not None
    assert await coordinator.join_writers(holder="replica-a", ttl_s=60.0) is None
    assert await coordinator.writers() == ()


@pytest.mark.asyncio
async def test_a_live_writer_refuses_the_backup(tmp_path) -> None:
    coordinator = FileCoordination(root=tmp_path)
    writer = await coordinator.join_writers(holder="replica-a", ttl_s=60.0)
    assert writer is not None
    assert await coordinator.enter_maintenance(holder="backup", ttl_s=60.0) is None
    assert await coordinator.writers() == ("replica-a",)


@pytest.mark.asyncio
async def test_an_expired_writer_does_not_block_the_backup(tmp_path) -> None:
    now = [1000.0]
    coordinator = FileCoordination(root=tmp_path, clock=lambda: now[0])
    writer = await coordinator.join_writers(holder="replica-a", ttl_s=30.0)
    assert writer is not None
    now[0] += 31.0
    # The heartbeat stopped (a crashed replica), so the lease is dead rather than
    # held: a backup that waited for an operator to clear a stale file would never
    # run after a crash.
    assert await coordinator.writers() == ()
    assert await coordinator.enter_maintenance(holder="backup", ttl_s=60.0) is not None


@pytest.mark.asyncio
async def test_leaving_releases_the_writer_role(tmp_path) -> None:
    coordinator = FileCoordination(root=tmp_path)
    writer = await coordinator.join_writers(holder="replica-a", ttl_s=60.0)
    assert writer is not None
    await coordinator.leave_writers(writer)
    assert await coordinator.writers() == ()
    assert await coordinator.enter_maintenance(holder="backup", ttl_s=60.0) is not None
