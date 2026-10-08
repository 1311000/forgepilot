"""Fixtures for the HTTP surface.

Two clients, because the transport decides what can be tested:

``api``
    ``httpx.ASGITransport`` — fast, in-process, no socket. Exercises the real
    routing, authentication and serialisation.

``live_api``
    A real ``uvicorn`` server on an ephemeral loopback port.

The second one is not a luxury. **``ASGITransport`` buffers the whole response
body before returning**, so an event stream that never ends can never be read
through it — the request simply hangs. That is a property of the test transport,
not of the endpoint, and using it here would make the SSE tests fail for a reason
that has nothing to do with the code under test.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the `web` extra is not installed")

KEY = b"test-signing-key"
TOKEN = "tok-alice"
OTHER_TOKEN = "tok-bob"


def _tokens() -> str:
    return json.dumps(
        [
            {
                "token": TOKEN,
                "principal_id": "alice",
                "tenant_id": "acme",
                "teams": ["team-a"],
                "operations": [
                    "read_code",
                    "write_code",
                    "run_tests",
                    "open_pr",
                    "read_ticket",
                ],
                "repo_prefixes": ["git.corp/team/*"],
                "writable_paths": ["src"],
            },
            {
                "token": OTHER_TOKEN,
                "principal_id": "bob",
                "tenant_id": "acme",
                "teams": ["team-b"],
                "operations": ["read_code"],
                "repo_prefixes": ["git.corp/team/*"],
                "writable_paths": ["src"],
            },
        ]
    )


@dataclass
class ApiRig:
    client: object
    root: object
    registry: object
    app: object
    workspace: Path
    llm: object
    sandbox: object
    approval: object

    def auth(self, token: str = TOKEN) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


class _Built:
    """A booted container and the app over it, before any client exists."""

    def __init__(self, root, fibers, app, workspace) -> None:
        self.root = root
        self.fibers = fibers
        self.app = app
        self.workspace = workspace

    def rig(self, client) -> ApiRig:
        return ApiRig(
            client=client,
            root=self.root,
            registry=self.app.state.registry,
            app=self.app,
            workspace=self.workspace,
            llm=self.root.llm,
            sandbox=self.root.sandbox,
            approval=self.root.approval,
        )

    async def close(self) -> None:
        from forgepilot.kernel import dispose_fiber

        await self.app.state.registry.close()
        for fiber in reversed(self.fibers):
            await dispose_fiber(fiber)


async def _boot(tmp_path: Path) -> _Built:
    from forgepilot.config import resolve_plugin
    from forgepilot.httpapi.app import build_app
    from forgepilot.kernel import create_root

    root = create_root()
    fibers = []

    async def load(spec: str, config: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), config or {})
        fibers.append(fiber)
        return fiber

    for spec, config in [
        ("forgepilot.identity.principal:PrincipalServicePlugin", None),
        ("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin", None),
        # `full` enforcement so exec-capable tools can be armed; the arm phase's
        # refusal under weaker enforcement has its own tests elsewhere.
        ("forgepilot.providers.sandbox.recording:RecordingSandboxPlugin", {"enforcement": "full"}),
        ("forgepilot.runtime.tools.registry:ToolRegistryPlugin", None),
        ("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "ask"}),
        ("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin", None),
        ("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-1"}),
        ("forgepilot.runtime.loop.agent_loop:AgentFactoryRegistry", None),
        ("forgepilot.runtime.loop.agent_loop:ForgePilotAgentLoopPlugin", None),
    ]:
        await load(spec, config)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    # A real project file, so tests exercise the configured path. Without one the
    # app falls back to an open project policy (and says so on stderr), which
    # would make these tests a check of the demo default rather than of the thing
    # a deployment actually runs.
    (workspace / "project.yaml").write_text(
        "name: svc-a\n"
        "repo_prefixes: ['git.corp/team/*']\n"
        "writable_paths: ['src']\n"
        "test: ['pytest -q']\n"
        "allowed_operations:\n"
        "  - read_code\n  - write_code\n  - run_tests\n  - open_pr\n  - read_ticket\n",
        encoding="utf-8",
    )

    app = build_app(root, workspace_root=workspace, approval_timeout=1.0)
    return _Built(root=root, fibers=fibers, app=app, workspace=workspace)


@pytest.fixture
def signing_env(monkeypatch):
    monkeypatch.setenv("FORGEPILOT_SIGNING_KEY", KEY.decode())
    monkeypatch.setenv("FORGEPILOT_DEV_TOKENS", _tokens())


@pytest.fixture
async def api(tmp_path: Path, signing_env):
    import httpx

    built = await _boot(tmp_path)
    transport = httpx.ASGITransport(app=built.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield built.rig(client)
    finally:
        await built.close()


@pytest.fixture
async def live_api(tmp_path: Path, signing_env):
    """The same app over a real loopback socket, so streams can actually stream."""
    import httpx
    import uvicorn

    built = await _boot(tmp_path)
    config = uvicorn.Config(
        built.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())

    try:
        deadline = asyncio.get_running_loop().time() + 10.0
        while not server.started:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the test server never started")
            await asyncio.sleep(0.01)

        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            yield built.rig(client)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            serving.cancel()
        await built.close()


@pytest.fixture
async def session_id(api) -> str:
    """A session created through the in-process client, owned by alice."""
    response = await api.client.post("/sessions", headers=api.auth())
    assert response.status_code == 201, response.text
    return response.json()["sessionId"]


@pytest.fixture
def session_id_factory():
    """Create a session through whichever client a test is using."""

    async def create(rig) -> str:
        response = await rig.client.post("/sessions", headers=rig.auth())
        assert response.status_code == 201, response.text
        return response.json()["sessionId"]

    return create
