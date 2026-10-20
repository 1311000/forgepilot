"""Fixtures for the session-runtime suites."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.seams.refs import SessionId
from forgepilot.seams.session import SessionHeader


@pytest.fixture(params=["memory", "jsonl"])
async def store(request, tmp_path: Path):
    """Both store implementations, so runtime behaviour is not store-specific."""
    from forgepilot.config import resolve_plugin

    spec = {
        "memory": "forgepilot.providers.session_store.memory:InMemorySessionStorePlugin",
        "jsonl": "forgepilot.providers.session_store.jsonl:JsonlSessionStorePlugin",
    }[request.param]
    config = {"root": str(tmp_path / "sessions")} if request.param == "jsonl" else {}

    root = create_root()
    fiber = await root.plugin(resolve_plugin(spec), config)
    try:
        yield root.sessionPersistence
    finally:
        await dispose_fiber(fiber)


@pytest.fixture
async def created(store):
    """A store plus a session id that already exists in it."""
    sid = SessionId("s-under-test")
    await store.create(SessionHeader(session_id=sid, created_at=datetime.now(UTC), tenant="acme"))
    return store, sid
