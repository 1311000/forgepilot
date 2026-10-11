"""Shared fixtures for the seam contract suites.

A contract suite is written **once per seam** and run against **every** provider
of that seam. Right now each seam has a single mock provider; when the real ones
land, they get added to the parametrisation and must satisfy the same suite. That
is the point of writing the tests at the seam rather than against the mock.
"""

from __future__ import annotations

import pytest

from forgepilot.kernel import create_root, dispose_fiber

#: Every mock provider, paired with the config it needs.
MOCK_PROVIDERS: tuple[tuple[str, dict], ...] = (
    ("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-test"}),
    ("forgepilot.providers.retrieve.mock:MockRetrieverPlugin", {"max_hits": 50}),
    ("forgepilot.providers.tickets.mock:MockTicketSourcePlugin", {}),
    ("forgepilot.providers.vcs.mock:MockVcsPlugin", {}),
    ("forgepilot.providers.workspace.local:LocalTempWorkspacePlugin", {}),
    ("forgepilot.providers.sandbox.recording:RecordingSandboxPlugin", {"enforcement": "none"}),
    ("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin", {}),
    ("forgepilot.providers.memory.memory:InMemoryMemoryStorePlugin", {}),
    # No `path`: the watermark file is created on the first `set`, so loading this
    # row in a test writes nothing anywhere (see providers/distillation/watermark.py).
    ("forgepilot.providers.distillation.watermark:JsonlWatermarkPlugin", {}),
    ("forgepilot.providers.approval.scripted:ScriptedApprovalPlugin", {"policy": "ask"}),
    (
        "forgepilot.providers.credentials.env:EnvCredentialPlugin",
        {"entries": {"demo_token": {"env": "FORGEPILOT_TEST_TOKEN"}}},
    ),
)


@pytest.fixture
async def container():
    """A root context with every mock provider loaded under its seam service name.

    Loading through the real container rather than instantiating classes directly
    means the suites exercise the same registration path the runtime uses — a
    provider that only works when constructed by hand is not a working provider.
    """
    from forgepilot.config import resolve_plugin

    root = create_root()
    fibers = []

    for spec, config in MOCK_PROVIDERS:
        fiber = await root.plugin(resolve_plugin(spec), config)
        fibers.append(fiber)

    try:
        yield root
    finally:
        for fiber in reversed(fibers):
            await dispose_fiber(fiber)


#: Every provider of the ``memory`` seam, paired with the config it needs. The record
#: suite runs against **all** of them, because a store that satisfies the rules only
#: in process memory is a cache. ``container`` cannot carry both — one root provides
#: one ``memory`` — so the suite asks for ``memory_store`` instead.
MEMORY_PROVIDERS: tuple[tuple[str, dict], ...] = (
    ("forgepilot.providers.memory.memory:InMemoryMemoryStorePlugin", {}),
    ("forgepilot.providers.memory.sqlite:SqliteMemoryStorePlugin", {}),
)


@pytest.fixture(params=MEMORY_PROVIDERS, ids=["in-memory", "sqlite"])
async def memory_store(request, tmp_path):
    """One provider of the ``memory`` seam, alone in a root, with a scratch file.

    ``tmp_path`` is what makes the sqlite parameter honest: without it the second
    parameter writes into the user's home directory, and a suite that touches that is
    one nobody can run twice.
    """
    from forgepilot.config import resolve_plugin

    spec, config = request.param
    config = dict(config)
    if "sqlite" in spec:
        config["path"] = str(tmp_path / "memory.sqlite3")

    root = create_root()
    fiber = await root.plugin(resolve_plugin(spec), config)
    try:
        yield root.memory
    finally:
        await dispose_fiber(fiber)
