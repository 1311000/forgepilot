"""A fully wired tool pipeline for the M4 suites.

Everything here goes through the real container: the registry, the sandbox, the
approval service and the shipped tools are all registered as plugins, so the
tests exercise the same wiring production will use rather than a hand-assembled
approximation of it.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgepilot.config import resolve_plugin
from forgepilot.config.project import ProjectConfig
from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.runtime.guards import GuardRegistry, UsageTracker, default_guards
from forgepilot.runtime.session import Session
from forgepilot.runtime.tools.pipeline import ToolPipeline
from forgepilot.seams.refs import PrincipalId, RepoRef, SessionId, TenantId
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal
from forgepilot.seams.session import SessionHeader


@pytest.fixture(autouse=True)
def _the_environment_a_test_leaves_behind_is_the_one_it_found():
    """Restore ``os.environ`` around every test.

    Not a convenience: a test that calls :func:`forgepilot.env.load_local_env` changes the
    process for every test after it, and the failure that follows is attributed to whichever
    test happens to run next. Restoring here makes the leak impossible to express.
    """
    before = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


# ---------------------------------------------------------------------------
# A temp root inside the checkout, decided rather than inherited.
#
# ⚠️ **Python's last-resort temp directory is the current directory, and on this
# development host that resort is what happens.** Measured
# (`tempfile._candidate_tempdir_list()` walked one candidate at a time):
# `TMPDIR`/`TEMP`/`TMP` name `C:\Users\<user>\AppData\Local\Temp`, and every real
# candidate answers `PermissionError: [Errno 13] Permission denied` on `mkstemp` —
# that path, `C:\Windows\Temp`, `c:\tmp` — while the rest (`c:\temp`, `\temp`,
# `\tmp`) do not exist. The only creatable candidate is the repository root, so
# `tempfile.gettempdir()` returns it, and a bare `pytest` run then drops its
# `tmp_path` trees (thousands of files) and ForgePilot's own `forgepilot-*`
# checkouts into the front door of the project. `.gitignore` already ignores both;
# an ignore rule is the belt, not the fix.
#
# Deciding it here rather than in a shell profile is deliberate: this is the one
# place a test run cannot forget, and it is imported before pytest builds its
# basetemp. The directory stays *inside* the checkout on purpose —
# `tests/providers/test_grep_retriever.py` records that this host's temporary files
# are inside the repository, and moving them outside would silently change which
# branch of that test runs.
# ---------------------------------------------------------------------------
if Path(tempfile.gettempdir()).resolve() == Path(__file__).resolve().parents[1]:
    _TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "tmp"
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(_TMP_ROOT)
    for _variable in ("TMPDIR", "TEMP", "TMP"):
        os.environ[_variable] = str(_TMP_ROOT)

#: The caller every rig builds its pipeline for. A pipeline now requires one:
#: `tools/pre-execute` asks the policy service *who is calling*, and a permission
#: check that cannot say who is not one.
RIG_PRINCIPAL = Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme"))


@dataclass
class Rig:
    """Everything a pipeline test needs to drive and inspect one call."""

    root: object
    pipeline: ToolPipeline
    session: Session
    store: object
    sandbox: object
    approval: object
    registry: object
    tracker: UsageTracker
    guards: GuardRegistry
    workspace: Path
    credentials: object = None


# ⚠️ A second `constraints_for` used to live here, taking a `workspace` plus
# `repos`/`prefixes`. The one further down takes the same name, so this one was
# **shadowed from the day it was written** and could never be called — `ruff`
# reported it as F811 the first time a linter ran over this repository.
#
# Deleted rather than renamed because nothing referenced it: the only call site in
# this file uses the signature below, and no module imports this one. If a richer
# helper is wanted later, give it a name that says what it adds rather than
# reusing this one — a shadowed helper reads as available and is not.


@dataclass
class LoopRig:
    """A pipeline plus a scripted model, enough to drive turns end to end."""

    root: object
    session: Session
    llm: object
    pipeline: ToolPipeline
    registry: object
    tracker: UsageTracker
    workspace: Path


@pytest.fixture
async def loop_rig(tmp_path: Path):
    """Session + ReplayLLM + tool pipeline, wired as the loop expects.

    The model is scripted rather than live: a loop test that calls a real endpoint
    is slow, non-deterministic and needs credentials, and it would hide
    prompt-assembly bugs behind the model's tolerance. No test in this suite
    touches a network.
    """
    from datetime import datetime

    from forgepilot.seams.session import SessionHeader

    root = create_root()
    fibers = []

    async def load(spec: str, config: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), config or {})
        fibers.append(fiber)
        return fiber

    await load("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin")
    await load(
        "forgepilot.providers.sandbox.recording:RecordingSandboxPlugin",
        {"enforcement": "full"},
    )
    await load("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-1"})
    await load("forgepilot.runtime.tools.registry:ToolRegistryPlugin")
    await load("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "ask"})
    await load("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin")

    tracker = UsageTracker()
    guards = GuardRegistry()
    for name, guard in default_guards(tracker):
        guards.register(name, guard)

    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "a.py").write_text("def retry(): ...\n", encoding="utf-8")

    pipeline = ToolPipeline(
        registry=root.tools,
        guards=guards,
        approval=root.approval,
        enforcement=root.sandbox.probe,
        principal=RIG_PRINCIPAL,
        workspace_root=workspace,
    )
    root.tools.attach_pipeline(pipeline)

    session_id = SessionId("loop-session")
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=session_id,
            created_at=datetime.now(UTC),
            tenant="acme",
            principal="alice",
        )
    )
    session = await Session.open(root.sessionPersistence, session_id)

    try:
        yield LoopRig(
            root=root,
            session=session,
            llm=root.llm,
            pipeline=pipeline,
            registry=root.tools,
            tracker=tracker,
            workspace=workspace,
        )
    finally:
        for fiber in reversed(fibers):
            await dispose_fiber(fiber)


@pytest.fixture
async def rig(tmp_path: Path):
    """A booted pipeline with one open session."""
    from datetime import datetime

    root = create_root()
    fibers = []

    async def load(spec: str, config: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), config or {})
        fibers.append(fiber)
        return fiber

    await load("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin")
    # `full` enforcement so that exec-capable tools can be armed; the arm phase's
    # refusal under weaker enforcement has its own test.
    await load(
        "forgepilot.providers.sandbox.recording:RecordingSandboxPlugin",
        {"enforcement": "full"},
    )
    await load("forgepilot.runtime.tools.registry:ToolRegistryPlugin")
    await load("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "ask"})
    await load("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin")
    # `code_search` is its own plugin because it needs a different service, so
    # the dedicated tool suite loads it explicitly. A rig that does not care about
    # search is not made to carry it.
    # ⚠️ **Deliberately not the workspace.** The configured root is where a
    # repository *lives*; the session works in a checkout cut from it, and the two
    # diverge the moment anything is edited. This rig used to point both at
    # `tmp_path / "ws"`, which made "search the configured path" and "search the
    # working tree" the same instruction — so no test in the suite could tell
    # them apart, and a real run found out the hard way (a bare mirror, `no
    # match`, and a model that went looking with a shell instead).
    await load(
        "forgepilot.providers.retrieve.grep:GrepRetrieverPlugin",
        {"roots": {REPO.slug: str(tmp_path / "checkout")}},
    )
    await load("forgepilot.runtime.tools.builtin:CodeSearchToolPlugin")
    # The credential channel, with one declarable name. The *value* is read from
    # the environment at resolve time and is set by the test that needs it, so no
    # secret ever lives in this file.
    await load(
        "forgepilot.providers.credentials.env:EnvCredentialPlugin",
        {
            "entries": {
                "demo_token": {
                    "env": "FORGEPILOT_TEST_TOKEN",
                    "description": "the token a credential test asks for by name",
                }
            }
        },
    )

    tracker = UsageTracker()
    guards = GuardRegistry()
    for name, guard in default_guards(tracker):
        guards.register(name, guard)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "src").mkdir()

    pipeline = ToolPipeline(
        registry=root.tools,
        guards=guards,
        approval=root.approval,
        enforcement=root.sandbox.probe,
        principal=RIG_PRINCIPAL,
        workspace_root=workspace,
        credentials=root.credentials,
    )
    root.tools.attach_pipeline(pipeline)

    session_id = SessionId("s-under-test")
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=session_id,
            created_at=datetime.now(UTC),
            tenant="acme",
            principal="alice",
        )
    )
    session = await Session.open(root.sessionPersistence, session_id)

    try:
        yield Rig(
            root=root,
            pipeline=pipeline,
            session=session,
            store=root.sessionPersistence,
            sandbox=root.sandbox,
            approval=root.approval,
            registry=root.tools,
            tracker=tracker,
            guards=guards,
            workspace=workspace,
            credentials=root.credentials,
        )
    finally:
        for fiber in reversed(fibers):
            await dispose_fiber(fiber)


@pytest.fixture
def constraints_factory():
    """Build ``ExecutionConstraints`` from a test.

    A fixture rather than an importable helper on purpose: pytest puts a test
    file's own directory on ``sys.path``, not the tests package, so
    ``from .conftest import ...`` fails and ``from ..conftest import ...`` fails
    too. Fixtures are shared across subdirectories without any import at all,
    which is why every shared test helper that is not a fixture should be one.
    """
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import (
        ExecutionConstraints,
        NetworkAccess,
        Principal,
    )

    def make(*operations, writable=("src",), repos=(), network=NetworkAccess.DENY):
        return ExecutionConstraints.for_principal(
            Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
            allowed_repos=frozenset(repos),
            permitted_operations=frozenset(operations),
            writable_paths=tuple(writable),
            network=network,
        )

    return make


# ---------------------------------------------------------------------------
# The task-mode rig, shared so the review suite can drive the same flow.
# ---------------------------------------------------------------------------
REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")

#: What a checkout of the project would look like before the agent touches it.
SEED = {
    "src/login.py": "MAX_ATTEMPTS = 3\n\n\ndef retry_login(client, credentials):\n    attempts = 0\n    while True:\n        try:\n            return client.login(credentials)\n        except AuthError:\n            attempts += 1\n",
    "tests/test_login.py": "def test_retry_is_bounded():\n    assert False, 'retry loop is unbounded'\n",
}


@dataclass
class TaskRig:
    root: object
    session: Session
    llm: object
    registry: object
    pipeline: ToolPipeline
    sandbox: object
    vcs: object
    workspace: object
    #: The decision authority and the answerer. `run_task` needs them directly:
    #: the outward-facing step — push and open a pull request — is not a tool
    #: call, so nothing in the pipeline ever sees it.
    policy: object
    approval: object
    principal: object
    tracker: UsageTracker
    constraints: ExecutionConstraints
    project: ProjectConfig

    async def close(self) -> None:
        await self.session.close()


def constraints_for(*operations: Operation, writable=("src",), repos=None) -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        # The task flow reaches the repository through the VCS seam, which
        # re-checks the repo allow-list itself — so an unset list is a refusal,
        # not an unrestricted default.
        allowed_repos=frozenset(repos) if repos is not None else frozenset({REPO}),
        permitted_operations=frozenset(operations),
        writable_paths=writable,
        network=NetworkAccess.DENY,
    )


PROJECT = ProjectConfig(
    name="svc-a",
    repos=(REPO,),
    repo_prefixes=("git.corp/team/*",),
    default_branch="main",
    test=("pytest -q",),
    allowed_operations=frozenset(Operation),
    # `tests` is writable so a test can express the thing the pinning exists to
    # rule out: an agent that edits the test instead of the code. With only
    # `src` writable the guard refuses the write, the tree never changes, and a
    # test of the pinning proves nothing while looking like it does.
    writable_paths=("src", "tests"),
    tool_sets={
        "default": ("read_file", "code_search"),
        "bugfix": ("read_file", "code_search", "write_file", "run"),
    },
    category_tool_sets={"bug": "bugfix"},
)

# ---------------------------------------------------------------------------
# A ticket that spans several repositories (M13).
# ---------------------------------------------------------------------------
REPO_B = RepoRef(host="git.corp", namespace="team", name="svc-b")
REPO_C = RepoRef(host="git.corp", namespace="team", name="svc-c")

#: The second repository's seed, holding a file the first one does not have — so a
#: test can prove a delivery really stood in *its own* tree rather than in whichever
#: checkout happened to be prepared first.
SEED_B = {
    "src/report.py": "def summarise(rows):\n    return sum(rows)\n",
    "tests/test_report.py": "def test_summarise():\n    assert False, 'summarise is not written yet'\n",
}

SEED_C = {
    "src/schedule.py": "def next_slot(now):\n    return now\n",
    "tests/test_schedule.py": "def test_next_slot():\n    assert False, 'next_slot is not written yet'\n",
}

CROSS_PROJECT = ProjectConfig(
    name="team-suite",
    repos=(REPO, REPO_B, REPO_C),
    repo_prefixes=("git.corp/team/*",),
    default_branch="main",
    test=("pytest -q",),
    allowed_operations=frozenset(Operation),
    writable_paths=("src", "tests"),
    tool_sets={
        "default": ("read_file", "code_search"),
        "bugfix": ("read_file", "code_search", "write_file", "run"),
    },
    category_tool_sets={"bug": "bugfix"},
)


def make_origin_repo(root: Path, files: dict[str, str]) -> Path:
    """A real git repository whose ``main`` holds ``files``.

    Real rather than seeded: the point of the worktree provider is that the
    session's checkout *is* a checkout, and a fixture that faked the repository
    would put the lie back one layer down.
    """
    import subprocess

    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def run(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@test", *args],
            cwd=str(root),
            capture_output=True,
            check=True,
        )

    run("init", "-b", "main")
    run("add", "-A")
    run("commit", "-m", "initial")
    return root


@asynccontextmanager
async def _task_rig(
    tmp_path: Path,
    *,
    project: ProjectConfig,
    origins: dict[str, dict[str, str]],
    constraints: ExecutionConstraints,
):
    """A booted kernel with the task-mode services, over real git checkouts.

    `origins` maps each repository slug to the files its `main` holds, so one rig
    can stand up as many repositories as a test needs. M13 made a ticket spanning
    several repositories a supported shape, and a fixture that could only build one
    would leave that path untestable end to end.
    """
    root = create_root()
    fibers = []

    async def load(spec: str, config: dict | None = None):
        fiber = await root.plugin(resolve_plugin(spec), config or {})
        fibers.append(fiber)
        return fiber

    await load("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin")
    await load(
        "forgepilot.providers.sandbox.scripted:ScriptedSandboxPlugin",
        {"enforcement": "full"},
    )
    await load("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-1"})
    await load("forgepilot.runtime.tools.registry:ToolRegistryPlugin")
    await load("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "ask"})
    # `allow`, deliberately: the *grant* is what denies, and `run_task` consults
    # policy only for the outward-facing step. A rig configured `deny` would
    # refuse every publish and every tool call, which is the shipped-config trap
    # `tests/identity/test_policy_is_wired.py` exists to catch.
    await load("forgepilot.identity.policy:PolicyServicePlugin", {"default": "allow"})
    await load("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin")
    await load("forgepilot.providers.vcs.mock:MockVcsPlugin", {})
    # A **real** repository with a **real** worktree, because `commit` now lives on
    # the workspace seam and the temp-directory provider refuses it — correctly:
    # it has no history to commit to, and inventing a sha would put a fabricated
    # commit in the session log as evidence. One per slug, in the order the caller
    # declared them, so the ticket's order and the trees stay tellable apart.
    checkouts = {
        slug: str(make_origin_repo(tmp_path / f"origin-{index}", files))
        for index, (slug, files) in enumerate(origins.items())
    }
    await load(
        "forgepilot.providers.workspace.git_worktree:GitWorktreeWorkspacePlugin",
        {"repos": checkouts, "worktree_root": str(tmp_path / "worktrees")},
    )

    tracker = UsageTracker()
    guards = GuardRegistry()
    for name, guard in default_guards(tracker):
        guards.register(name, guard)

    pipeline = ToolPipeline(
        registry=root.tools,
        guards=guards,
        approval=root.approval,
        enforcement=root.sandbox.probe,
        principal=RIG_PRINCIPAL,
    )
    root.tools.attach_pipeline(pipeline)

    session_id = SessionId("task-session")
    await root.sessionPersistence.create(
        SessionHeader(
            session_id=session_id,
            created_at=datetime.now(UTC),
            tenant="acme",
            principal="alice",
        )
    )

    try:
        yield TaskRig(
            root=root,
            session=await Session.open(root.sessionPersistence, session_id),
            llm=root.llm,
            registry=root.tools,
            pipeline=pipeline,
            sandbox=root.sandbox,
            vcs=root.vcs,
            workspace=root.workspace,
            policy=root.policy,
            approval=root.approval,
            principal=RIG_PRINCIPAL,
            tracker=tracker,
            constraints=constraints,
            project=project,
        )
    finally:
        for fiber in reversed(fibers):
            await dispose_fiber(fiber)


@pytest.fixture
async def task_rig(tmp_path: Path):
    async with _task_rig(
        tmp_path,
        project=PROJECT,
        origins={REPO.slug: SEED},
        constraints=constraints_for(
            Operation.READ_CODE, Operation.WRITE_CODE, Operation.RUN_TESTS, Operation.OPEN_PR
        ),
    ) as rig:
        yield rig


@pytest.fixture
async def cross_repo_task_rig(tmp_path: Path):
    """The same rig with three repositories, all of them permitted (M13)."""
    async with _task_rig(
        tmp_path,
        project=CROSS_PROJECT,
        origins={REPO.slug: SEED, REPO_B.slug: SEED_B, REPO_C.slug: SEED_C},
        constraints=constraints_for(
            Operation.READ_CODE,
            Operation.WRITE_CODE,
            Operation.RUN_TESTS,
            Operation.OPEN_PR,
            repos=(REPO, REPO_B, REPO_C),
        ),
    ) as rig:
        yield rig
