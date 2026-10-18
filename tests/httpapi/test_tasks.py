"""Task mode over HTTP: start a ticket, read it back, survive a restart.

The API adds no execution path of its own, so these tests are about the three
things it *does* add:

- a ticket starts a task in the background, and the request returns before it
  finishes (202, not 200);
- what happened afterwards is read from the session log — including after a
  restart, when the registry that ran it is gone;
- a failure in the background is written down. A task that raises where nobody is
  listening must not look like a task that simply stopped.

The run below is the same one `tests/task/` drives through `run_task`: a scripted
model writes one file, a scripted sandbox passes the check, and the repository is
a real git checkout so the commit is real.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forgepilot.kernel import PluginService
from forgepilot.seams.classifier import SERVICE_NAME as CLASSIFIER_SERVICE

REMOTE = "git.corp/team/svc-a"
TICKET_ID = "BUG-1"

#: The whole change, as a scripted model turn. `write_file` is one of the tools the
#: `bugfix` set names in the project written below.
WRITE_FIX = '{"path": "src/app.py", "content": "VALUE = 2\\n"}'


def _git_repo(path: Path) -> Path:
    """A real repository, because the worktree provider commits to real history."""
    (path / "src").mkdir(parents=True)
    (path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    def run(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@test", *args],
            cwd=str(path),
            capture_output=True,
            check=True,
        )

    run("init", "-b", "main")
    run("add", "-A")
    run("commit", "-m", "initial")
    return path


def _write_ticket(path: Path, ticket_id: str, *, description: str) -> None:
    path.write_text(
        f"id: {ticket_id}\n"
        f"title: move the window one place\n"
        f"priority: high\n"
        f"description: |\n"
        + "".join(f"  {line}\n" for line in description.splitlines()),
        encoding="utf-8",
    )


class _Confident(PluginService):
    """A classifier that is sure, so the plan actually becomes a task.

    Injected as a class rather than resolved from a module, because there is no
    shipped plugin that says "always confident" — and inventing one would be a
    production seam answering a test's question.
    """

    provide = CLASSIFIER_SERVICE

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    async def classify(self, request: Any, *, session: Any = None) -> Any:
        from forgepilot.seams.classifier import TicketUnderstanding

        return TicketUnderstanding(category="bug", confidence=0.93)


class _Booted:
    def __init__(self, root: Any, fibers: list[Any], app: Any, workspace: Path, origin: Path):
        self.root = root
        self.fibers = fibers
        self.app = app
        self.workspace = workspace
        self.origin = origin

    def auth(self, token: str = "tok-alice") -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def close(self) -> None:
        from forgepilot.kernel import dispose_fiber

        await self.app.state.registry.close()
        for fiber in reversed(self.fibers):
            await dispose_fiber(fiber)


async def _boot(tmp_path: Path, *, with_project: bool = True) -> _Booted:
    from forgepilot.config import resolve_plugin
    from forgepilot.httpapi.app import build_app
    from forgepilot.kernel import create_root

    root = create_root()
    fibers: list[Any] = []

    async def load(spec: Any, config: dict | None = None) -> Any:
        fiber = await root.plugin(
            spec if not isinstance(spec, str) else resolve_plugin(spec), config or {}
        )
        fibers.append(fiber)
        return fiber

    origin = _git_repo(tmp_path / "origin")
    tickets = tmp_path / "tickets"
    tickets.mkdir()
    # Complete by the rules `preprocess` reads: reproduction, expected, actual.
    _write_ticket(
        tickets / f"{TICKET_ID}.yaml",
        TICKET_ID,
        description=(
            "复现步骤：\n调用 moving_average([1, 2, 3, 4], 2)\n"
            "预期结果：3 个窗口\n实际结果：只有 2 个窗口\n"
            f"受影响仓库：{REMOTE}"
        ),
    )
    _write_ticket(tickets / "T-EMPTY.yaml", "T-EMPTY", description="")

    for spec, config in [
        ("forgepilot.identity.principal:PrincipalServicePlugin", None),
        ("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin", None),
        ("forgepilot.providers.sandbox.scripted:ScriptedSandboxPlugin", {"enforcement": "full"}),
        ("forgepilot.runtime.tools.registry:ToolRegistryPlugin", None),
        ("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "ask"}),
        # `allow` because the grant is what denies; `ask_operations` is deliberately
        # absent so publishing does not wait for a human who is not there.
        ("forgepilot.identity.policy:PolicyServicePlugin", {"default": "allow"}),
        ("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin", None),
        ("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-1"}),
        ("forgepilot.runtime.loop.agent_loop:AgentFactoryRegistry", None),
        ("forgepilot.runtime.loop.agent_loop:ForgePilotAgentLoopPlugin", None),
        ("forgepilot.providers.tickets.file:FileTicketSourcePlugin", {"root": str(tickets)}),
        ("forgepilot.providers.vcs.mock:MockVcsPlugin", {}),
        (
            "forgepilot.providers.workspace.git_worktree:GitWorktreeWorkspacePlugin",
            {"repos": {REMOTE: str(origin)}, "worktree_root": str(tmp_path / "worktrees")},
        ),
    ]:
        await load(spec, config)
    await load(_Confident, {})

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    if with_project:
        (workspace / "project.yaml").write_text(
            "name: svc-a\n"
            f"repos:\n  - {REMOTE}\n"
            "repo_prefixes: ['git.corp/team/*']\n"
            "default_branch: main\n"
            "test: ['python -c pass']\n"
            "allowed_operations:\n"
            "  - read_code\n  - write_code\n  - run_tests\n  - read_ticket\n  - open_pr\n"
            "writable_paths: ['src']\n"
            "tool_sets:\n"
            "  default: [read_file, code_search]\n"
            "  bugfix: [read_file, code_search, write_file, run]\n"
            "category_tool_sets:\n"
            "  bug: bugfix\n",
            encoding="utf-8",
        )

    app = build_app(root, workspace_root=workspace, approval_timeout=1.0)
    return _Booted(root=root, fibers=fibers, app=app, workspace=workspace, origin=origin)


async def _client(booted: _Booted):
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=booted.app), base_url="http://test")


TERMINAL = frozenset({"ready", "delivered", "failed", "cancelled", "awaiting_info"})


async def _settle(client: Any, task_id: str, headers: dict[str, str]) -> dict[str, Any]:
    """Poll the task until it reaches a terminal state, or fail with what it said."""
    deadline = asyncio.get_running_loop().time() + 30.0
    last: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/tasks/{task_id}", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()
        if last["state"] in TERMINAL:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"the task never settled; last view: {last}")


# ---------------------------------------------------------------------------
# Starting
# ---------------------------------------------------------------------------
async def test_a_ticket_runs_and_its_delivery_is_read_from_the_log(
    tmp_path: Path, signing_env
) -> None:
    """The whole point of the endpoint: a ticket in, the log out."""
    booted = await _boot(tmp_path)
    try:
        async with await _client(booted) as client:
            from forgepilot.providers.llm.replay import text_turn, tool_call_turn
            from forgepilot.providers.sandbox.scripted import ScriptedResult

            booted.root.llm.script(
                tool_call_turn("write_file", WRITE_FIX, call_id="c1"),
                text_turn("done"),
            )
            booted.root.sandbox.script(ScriptedResult(exit_code=0, stdout="1 passed"))

            response = await client.post(
                "/tasks", json={"ticketId": TICKET_ID}, headers=booted.auth()
            )
            assert response.status_code == 202, response.text
            started = response.json()
            assert started["taskId"] == started["sessionId"], "one identity, not two"

            view = await _settle(client, started["taskId"], booted.auth())

            assert view["state"] == "delivered", view["reason"]
            assert view["ticketId"] == TICKET_ID
            assert view["interrupted"] is False
            assert len(view["repositories"]) == 1
            delivery = view["repositories"][0]
            assert delivery["repo"] == REMOTE
            assert delivery["branch"], "a delivery names its branch"
            assert delivery["commit"] and len(delivery["commit"]) == 40, delivery["commit"]
            assert delivery["changed"] == ["src/app.py"]
            assert delivery["pullRequest"], "a published delivery names the URL"
            assert delivery["checks"], "the check that decided the verdict is in the log"
    finally:
        await booted.close()


async def test_an_unknown_ticket_is_a_404_and_creates_nothing(tmp_path: Path, signing_env) -> None:
    booted = await _boot(tmp_path)
    try:
        async with await _client(booted) as client:
            before = (await client.get("/sessions", headers=booted.auth())).json()["sessions"]
            response = await client.post(
                "/tasks", json={"ticketId": "NOPE-9"}, headers=booted.auth()
            )
            assert response.status_code == 404
            assert "BUG-1" in response.text, "the refusal names the tickets that do exist"
            after = (await client.get("/sessions", headers=booted.auth())).json()["sessions"]
            assert after == before, "a ticket that does not exist must not open a session"
    finally:
        await booted.close()


async def test_task_mode_refuses_when_no_project_is_configured(
    tmp_path: Path, signing_env
) -> None:
    """Chat can fall back to an open policy; a task cannot, because it delivers."""
    booted = await _boot(tmp_path, with_project=False)
    try:
        async with await _client(booted) as client:
            response = await client.post(
                "/tasks", json={"ticketId": TICKET_ID}, headers=booted.auth()
            )
            assert response.status_code == 409
            assert "project.yaml" in response.text
    finally:
        await booted.close()


async def test_a_ticket_the_rules_reject_is_still_a_task_in_the_log(
    tmp_path: Path, signing_env
) -> None:
    """`plan_task` does not take the session, so the refusal has to be written here.

    Without it the log would hold a session that started and then said nothing —
    a panel would spin forever on a ticket a human has to complete.
    """
    booted = await _boot(tmp_path)
    try:
        async with await _client(booted) as client:
            response = await client.post(
                "/tasks", json={"ticketId": "T-EMPTY"}, headers=booted.auth()
            )
            assert response.status_code == 202, response.text
            view = await _settle(client, response.json()["taskId"], booted.auth())
            assert view["state"] == "awaiting_info"
            assert "description" in view["reason"]
            assert view["repositories"] == []
    finally:
        await booted.close()


# ---------------------------------------------------------------------------
# Reading it back after the process that ran it is gone
# ---------------------------------------------------------------------------
async def test_a_task_read_after_a_restart_keeps_its_state_and_its_owner(
    tmp_path: Path, signing_env
) -> None:
    """The registry is process memory; the session header and log are not."""
    from forgepilot.runtime.session import Session
    from forgepilot.seams.refs import SessionId
    from forgepilot.seams.session import EventKind, SessionHeader

    booted = await _boot(tmp_path)
    try:
        store = booted.root.sessionPersistence
        sid = SessionId("task-from-before")
        await store.create(
            SessionHeader(
                session_id=sid,
                created_at=datetime.now(UTC),
                tenant="acme",
                principal="alice",
            )
        )
        session = await Session.open(store, sid)
        await session.append(
            EventKind.TASK_STATE,
            {"state": "running", "ticket": "BUG-9", "repositories": [REMOTE]},
        )
        await session.append(
            EventKind.TASK_STATE,
            {
                "state": "ready",
                "repo": REMOTE,
                "branch": "forgepilot/bug-9",
                "commit": "a" * 40,
                "changed": ["src/app.py"],
                "pullRequest": None,
                "reason": "no repo host configured",
            },
        )
        await session.flush()

        # A second session that stopped before any delivery: the only thing its log
        # says is that it started.
        stopped = SessionId("task-that-stopped")
        await store.create(
            SessionHeader(
                session_id=stopped,
                created_at=datetime.now(UTC),
                tenant="acme",
                principal="alice",
            )
        )
        half = await Session.open(store, stopped)
        await half.append(
            EventKind.TASK_STATE,
            {"state": "running", "ticket": "BUG-10", "repositories": [REMOTE]},
        )
        await half.flush()

        # A second app over the same container: the registry it was started in is
        # gone, exactly as it would be after a restart.
        from forgepilot.httpapi.app import build_app

        restarted = build_app(booted.root, workspace_root=booted.workspace)
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as client:
            response = await client.get("/tasks/task-from-before", headers=booted.auth())
            assert response.status_code == 200, response.text
            view = response.json()
            assert view["state"] == "ready"
            assert view["ticketId"] == "BUG-9", "the ticket is read from the opening event"
            assert view["live"] is False
            assert view["interrupted"] is False, "a delivery is a state, not a silence"
            assert view["repositories"][0]["commit"] == "a" * 40
            assert view["repositories"][0]["changed"] == ["src/app.py"]

            half_view = (
                await client.get("/tasks/task-that-stopped", headers=booted.auth())
            ).json()
            assert half_view["state"] == "running"
            assert half_view["interrupted"] is True, "nobody is running it, and it says running"

            # Not alice's to read — and 404, not 403, so the API cannot be used to
            # discover that the id exists.
            other = await client.get("/tasks/task-from-before", headers=booted.auth("tok-bob"))
            assert other.status_code == 404
    finally:
        await booted.close()


async def test_a_chat_session_is_not_a_task(tmp_path: Path, signing_env) -> None:
    booted = await _boot(tmp_path)
    try:
        async with await _client(booted) as client:
            created = await client.post("/sessions", headers=booted.auth())
            session_id = created.json()["sessionId"]
            response = await client.get(f"/tasks/{session_id}", headers=booted.auth())
            assert response.status_code == 404
            listing = await client.get("/tasks", headers=booted.auth())
            assert listing.json()["tasks"] == []
    finally:
        await booted.close()


pytestmark = pytest.mark.skipif(
    __import__("shutil").which("git") is None, reason="the workspace provider needs git"
)
