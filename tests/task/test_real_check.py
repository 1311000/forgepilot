"""The repair loop against a sandbox that actually executes.

Everything here was previously unreachable: the only executing sandbox in the
tree was a scripted one, so `run_checks` was exercised against pre-recorded exit
codes and its real behaviour on a real command was never observed. Two things
fell out the moment it was:

- the configured commands were joined with ``&&`` and then handed to ``exec``
  **without a shell**, so everything after the first command arrived as literal
  arguments to it — invisible while nothing ran;
- with a real pytest, a failing check really fails and says why.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from forgepilot.runtime.repair import run_checks
from forgepilot.seams.refs import PrincipalId, RepoRef, SessionId, TenantId, TurnId
from forgepilot.seams.sandbox import SandboxEnforcement
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal


class _FixedClassifier:
    """Declines nothing and always says `bug`, so the plan can be built."""

    def __init__(self, understanding) -> None:
        self.understanding = understanding

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    async def classify(self, request):  # noqa: ANN001
        return self.understanding


#: The fix the scripted model writes. `REAL_SEED`'s test looks for exactly this.
REAL_FIX = (
    "MAX_ATTEMPTS = 3\n\n\ndef retry_login(client, credentials):\n"
    "    for _ in range(MAX_ATTEMPTS):\n        try:\n"
    "            return client.login(credentials)\n        except AuthError:\n"
    "            continue\n    raise RuntimeError('login failed after retries')\n"
)


class FakeSandbox:
    """Records argv and answers with a scripted exit code — for logic under test.

    A recording sandbox is the right tool for asking *what would have been run*.
    It is the wrong tool for asking whether the loop closes, which is why the
    execution tests below use a real one.
    """

    def __init__(self, codes: list[int] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._codes = list(codes or [])

    def probe(self):
        from forgepilot.seams.sandbox import SandboxEnforcement

        return SandboxEnforcement.NONE

    async def run(self, argv, *, policy, cwd, env, timeout_s, stdin=None):
        from forgepilot.seams.sandbox import ExecResult, SandboxEnforcement

        self.calls.append(tuple(argv))
        code = self._codes.pop(0) if self._codes else 0
        return ExecResult(
            argv=tuple(argv),
            exit_code=code,
            stdout=f"ran {argv[0]}",
            enforcement=SandboxEnforcement.NONE,
            executed=True,
        )


@pytest.fixture
def constraints() -> ExecutionConstraints:
    # ``network=ALLOW`` because this rig runs the *local* backend, which provides no
    # containment and therefore refuses a policy it cannot enforce — including a
    # requested network denial. Accepting an uncontained command means accepting
    # that it can reach the network; the alternative was a test that handed the
    # backend a policy it was right to refuse.
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        permitted_operations=frozenset({Operation.RUN_TESTS}),
        writable_paths=("src",),
        network=NetworkAccess.ALLOW,
    )


@pytest.fixture
async def session():
    """A live session, because run_checks records a ``task/check`` event."""
    from datetime import datetime

    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber
    from forgepilot.runtime.session import Session
    from forgepilot.seams.session import SessionHeader

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin"), {}
    )
    sid = SessionId("s-check")
    await root.sessionPersistence.create(
        SessionHeader(session_id=sid, created_at=datetime.now(UTC), tenant="acme")
    )
    live = await Session.open(root.sessionPersistence, sid)
    try:
        yield live
    finally:
        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# Each configured command is a command
# ---------------------------------------------------------------------------
async def test_every_configured_command_is_run_as_its_own_process(
    session, constraints, tmp_path
) -> None:
    """``&&`` is a shell operator. There is no shell here.

    The commands were joined into one string and ``shlex.split``, so
    ``pytest -q && mypy src`` became ``["pytest", "-q", "&&", "mypy", "src"]`` —
    the shell operator became a literal argument, and the second command was
    never run as a command at all. Nothing noticed, because the sandbox in the
    loop answered from a script and never looked at argv.
    """
    sandbox = FakeSandbox()

    await run_checks(
        commands=["first-check", "second-check"],
        sandbox=sandbox,
        session=session,
        constraints=constraints,
        workspace_root=tmp_path,
        session_id=session.header.session_id,
        turn=TurnId(1),
    )

    assert sandbox.calls == [("first-check",), ("second-check",)]


async def test_the_chain_stops_at_the_first_failure(session, constraints, tmp_path) -> None:
    """Later checks after a failure describe a state that was never reached."""
    sandbox = FakeSandbox(codes=[1, 0])

    result = await run_checks(
        commands=["first-check", "second-check"],
        sandbox=sandbox,
        session=session,
        constraints=constraints,
        workspace_root=tmp_path,
        session_id=session.header.session_id,
        turn=TurnId(1),
    )

    assert sandbox.calls == [("first-check",)], "the second check ran past a failure"
    assert result.passed is False
    assert result.exit_code == 1


async def test_all_checks_must_pass(session, constraints, tmp_path) -> None:
    sandbox = FakeSandbox(codes=[0, 0])

    result = await run_checks(
        commands=["first-check", "second-check"],
        sandbox=sandbox,
        session=session,
        constraints=constraints,
        workspace_root=tmp_path,
        session_id=session.header.session_id,
        turn=TurnId(1),
    )

    assert len(sandbox.calls) == 2
    assert result.passed is True


async def test_a_command_with_arguments_is_still_one_command(session, constraints, tmp_path) -> None:
    """Splitting the command line is right; splitting *between* commands is not."""
    sandbox = FakeSandbox()

    await run_checks(
        commands=["pytest -q tests/unit"],
        sandbox=sandbox,
        session=session,
        constraints=constraints,
        workspace_root=tmp_path,
        session_id=session.header.session_id,
        turn=TurnId(1),
    )

    assert sandbox.calls == [("pytest", "-q", "tests/unit")]


async def test_a_command_containing_a_path_survives_splitting(
    session, constraints, tmp_path, real_sandbox
) -> None:
    """A configured command may name an absolute interpreter, and must still run.

    ``shlex.split(posix=True)`` treats a backslash as an escape, so on Windows
    ``C:\\…\\python.exe -m pytest`` arrives as ``C:…python.exe`` — a binary that
    does not exist. The check then reports "could not run", which reads as a
    configuration problem rather than as the harness mangling its own argv.

    Production is Linux, where this passes either way; the dev machine is not,
    and a check that cannot run on the machine you develop on is a loop that
    never closes there. So the split is done the way the *platform* defines a
    command line rather than the way POSIX does.
    """
    sandbox = FakeSandbox()

    await run_checks(
        commands=[f"{sys.executable} -m pytest -q"],
        sandbox=sandbox,
        session=session,
        constraints=constraints,
        workspace_root=tmp_path,
        session_id=session.header.session_id,
        turn=TurnId(1),
    )

    assert sandbox.calls == [(sys.executable, "-m", "pytest", "-q")], (
        "the interpreter path was altered by splitting, so the check would not run"
    )


# ---------------------------------------------------------------------------
# The loop closes: a real command, a real result
# ---------------------------------------------------------------------------
REAL_SANDBOX = "forgepilot.providers.sandbox.local:LocalProcessSandboxPlugin"
REPO_SLUG = RepoRef(host="git.corp", namespace="team", name="svc-a").slug

FAILING_TEST = "def test_thing():\n    assert False, 'the retry loop is unbounded'\n"
PASSING_TEST = "def test_thing():\n    assert True\n"


@pytest.fixture
async def real_sandbox():
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(resolve_plugin(REAL_SANDBOX), {})
    try:
        yield root.sandbox
    finally:
        await dispose_fiber(fiber)


async def test_a_real_check_really_fails_and_says_why(
    real_sandbox, session, constraints, tmp_path
) -> None:
    """The acceptance criterion for the whole milestone, in one assertion:

    before this, `executed=False` meant every check was `could_not_run`, so the
    harness could change files but never establish that a change was right.
    """
    (tmp_path / "test_thing.py").write_text(FAILING_TEST, encoding="utf-8")

    result = await run_checks(
        commands=[f"{sys.executable} -m pytest -q"],
        sandbox=real_sandbox,
        session=session,
        constraints=constraints,
        workspace_root=tmp_path,
        session_id=session.header.session_id,
        turn=TurnId(1),
    )

    assert result.could_not_run is False, result.reason
    assert result.passed is False, "a failing test suite reported as passing"
    assert result.exit_code != 0
    assert "the retry loop is unbounded" in result.output, "the reason is not in the output"


async def test_a_real_check_passes_once_the_code_is_fixed(
    real_sandbox, session, constraints, tmp_path
) -> None:
    """And the other direction — otherwise "it runs now" could just mean "it
    fails now", which is not a loop closing, it is a loop that always says no."""
    (tmp_path / "test_thing.py").write_text(PASSING_TEST, encoding="utf-8")

    result = await run_checks(
        commands=[f"{sys.executable} -m pytest -q"],
        sandbox=real_sandbox,
        session=session,
        constraints=constraints,
        workspace_root=tmp_path,
        session_id=session.header.session_id,
        turn=TurnId(1),
    )

    assert result.could_not_run is False, result.reason
    assert result.passed is True, result.output
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# The acceptance test P1 owes: a whole task, decided by a real test runner
# ---------------------------------------------------------------------------
#: A real test: it reads the file the model is supposed to change. Not a stub —
#: if the model writes nothing, it fails; if it writes the fix, it passes. The
#: verdict comes from pytest, not from a queue.
REAL_SEED = {
    "src/login.py": (
        "MAX_ATTEMPTS = 3\n\n\ndef retry_login(client, credentials):\n"
        "    attempts = 0\n    while True:\n        try:\n"
        "            return client.login(credentials)\n        except AuthError:\n"
        "            attempts += 1\n"
    ),
    "tests/test_login.py": (
        "import pathlib\n\n"
        "SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'src' / 'login.py'\n\n\n"
        "def test_retry_is_bounded():\n"
        "    assert 'for _ in range(MAX_ATTEMPTS)' in SOURCE.read_text(encoding='utf-8')\n"
    ),
}


@pytest.fixture
async def real_task_rig(tmp_path):
    """The task rig, with a real sandbox and a test that inspects the real file."""

    from forgepilot.config import resolve_plugin
    from forgepilot.config.project import ProjectConfig
    from forgepilot.kernel import create_root, dispose_fiber
    from forgepilot.runtime.guards import GuardRegistry, UsageTracker, default_guards
    from forgepilot.runtime.session import Session
    from forgepilot.runtime.tools.pipeline import ToolPipeline
    from forgepilot.seams.refs import SessionId
    from forgepilot.seams.security import Operation
    from forgepilot.seams.session import SessionHeader

    root = create_root()
    fibers = []

    async def load(spec, config=None):
        fiber = await root.plugin(resolve_plugin(spec), config or {})
        fibers.append(fiber)

    await load("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin")
    await load("forgepilot.providers.sandbox.local:LocalProcessSandboxPlugin", {})
    await load("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-1"})
    await load("forgepilot.runtime.tools.registry:ToolRegistryPlugin")
    await load("forgepilot.runtime.approval:ApprovalServicePlugin", {"policy": "never"})
    await load("forgepilot.runtime.tools.builtin:BuiltinToolsPlugin")
    await load("forgepilot.providers.vcs.mock:MockVcsPlugin", {})
    # `allow`: the grant is what denies; policy's job is the call-time question.
    await load("forgepilot.identity.policy:PolicyServicePlugin", {"default": "allow"})
    from tests.conftest import make_origin_repo  # noqa: PLC0415 - see below

    origin = make_origin_repo(tmp_path / "origin", REAL_SEED)
    await load(
        "forgepilot.providers.workspace.git_worktree:GitWorktreeWorkspacePlugin",
        {"repos": {REPO_SLUG: str(origin)}, "worktree_root": str(tmp_path / "worktrees")},
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
        principal=Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        # The local sandbox reports NONE, so exec is refused unless the
        # requirement is lowered — the same explicit act the CLI asks for.
        minimum_enforcement_for_exec=SandboxEnforcement.NONE,
    )
    root.tools.attach_pipeline(pipeline)

    sid = SessionId("real-task")
    await root.sessionPersistence.create(
        SessionHeader(session_id=sid, created_at=datetime.now(UTC), tenant="acme", principal="alice")
    )

    project = ProjectConfig(
        name="svc-a",
        repos=(RepoRef(host="git.corp", namespace="team", name="svc-a"),),
        repo_prefixes=("git.corp/team/*",),
        # A real runner deciding a real file.
        test=(f"{sys.executable} -m pytest -q",),
        allowed_operations=frozenset(Operation),
        # "tests" is writable so a test can express the thing this suite exists
        # to rule out: an agent that edits the test instead of the code.
        writable_paths=("src", "tests"),
        tool_sets={"bugfix": ("read_file", "write_file")},
        category_tool_sets={"bug": "bugfix"},
    )

    try:
        yield SimpleNamespace(
            root=root,
            session=await Session.open(root.sessionPersistence, sid),
            llm=root.llm,
            registry=root.tools,
            pipeline=pipeline,
            sandbox=root.sandbox,
            vcs=root.vcs,
            workspace=root.workspace,
            tracker=tracker,
            policy=root.policy,
            approval=root.approval,
            principal=Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
            project=project,
            constraints=ExecutionConstraints.for_principal(
                Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
                allowed_repos=frozenset({RepoRef(host="git.corp", namespace="team", name="svc-a")}),
                permitted_operations=frozenset(
                    {Operation.READ_CODE, Operation.WRITE_CODE, Operation.RUN_TESTS, Operation.OPEN_PR}
                ),
                writable_paths=("src", "tests"),
                network=NetworkAccess.ALLOW,
            ),
        )
    finally:
        for fiber in reversed(fibers):
            await dispose_fiber(fiber)


async def test_a_whole_task_is_decided_by_a_real_test_runner(real_task_rig) -> None:
    """P1's acceptance criterion.

    Every earlier task-mode test proved the *flow* against a scripted sandbox:
    the check passed because a canned result said it did. Here the model edits a
    file, pytest reads that file, and the verdict is pytest's. If the model wrote
    nothing the check fails; if it wrote the fix the check passes — and nothing
    in the test tells the runner which.
    """
    import json

    from forgepilot.orchestration import ClassifierGateway
    from forgepilot.orchestration.modes.task import plan_task, run_task
    from forgepilot.providers.llm.replay import text_turn, tool_call_turn
    from forgepilot.seams.classifier import TicketUnderstanding
    from forgepilot.seams.refs import TicketId
    from forgepilot.seams.tickets import Ticket, TicketPriority

    rig = real_task_rig
    ticket = Ticket(
        id=TicketId("T-1"),
        title="登录无限重试",
        description_md="复现步骤：\n1. 输错密码\n\n预期结果：提示密码错误\n实际结果：无限重试",
        priority=TicketPriority.HIGH,
    )
    plan = await plan_task(
        ticket=ticket,
        project=rig.project,
        constraints=rig.constraints,
        registry=rig.registry,
        classifier=ClassifierGateway(
            _FixedClassifier(TicketUnderstanding(category="bug", symptom="无限重试", confidence=0.95))
        ),
    )
    assert plan.ready, plan.reason

    rig.llm.script(
        tool_call_turn(
            "write_file",
            json.dumps({"path": "src/login.py", "content": REAL_FIX}),
            call_id="c1",
        ),
        text_turn("给重试循环加上了上限"),
    )

    outcome = await run_task(
        plan=plan,
        project=rig.project,
        session=rig.session,
        llm=rig.llm,
        tools=rig.registry,
        vcs=rig.vcs,
        workspace=rig.workspace,
        sandbox=rig.sandbox,
        constraints=rig.constraints,
        pipeline=rig.pipeline,
        tracker=rig.tracker,
        model="replay-1",
        policy=rig.policy,
        approval=rig.approval,
        principal=rig.principal,
    )

    assert outcome.checks, "no check was recorded at all"
    final = outcome.checks[-1]
    assert final.could_not_run is False, final.reason
    assert final.passed is True, final.output
    assert outcome.delivered, outcome.reason


async def test_a_fix_that_does_not_address_the_check_leaves_the_task_undelivered(
    real_task_rig,
) -> None:
    """The other direction, and the one that makes the first test mean something.

    A check whose verdict never changes anything is decoration. Here the model
    tries twice and never writes what the test looks for, so pytest keeps failing
    and the repair loop keeps being asked — and the task is *not* delivered.

    This is also the assertion that caught the acceptance test passing for a
    reason it should not have: when the fix was mutated to something the seeded
    test does not look for, the loop requested a third turn and the script ran
    out. The verdict really does drive the loop, rather than merely being
    recorded beside it.
    """
    import json

    from forgepilot.orchestration import ClassifierGateway
    from forgepilot.orchestration.modes.task import plan_task, run_task
    from forgepilot.providers.llm.replay import text_turn, tool_call_turn
    from forgepilot.seams.classifier import TicketUnderstanding
    from forgepilot.seams.refs import TicketId
    from forgepilot.seams.tickets import Ticket, TicketPriority

    rig = real_task_rig
    ticket = Ticket(
        id=TicketId("T-1"),
        title="登录无限重试",
        description_md="复现步骤：" + chr(10) + "1. 输错密码" + chr(10) + chr(10)
        + "预期结果：提示密码错误" + chr(10) + "实际结果：无限重试",
        priority=TicketPriority.HIGH,
    )
    plan = await plan_task(
        ticket=ticket,
        project=rig.project,
        constraints=rig.constraints,
        registry=rig.registry,
        classifier=ClassifierGateway(
            _FixedClassifier(TicketUnderstanding(category="bug", symptom="retry", confidence=0.95))
        ),
    )
    assert plan.ready, plan.reason

    # Attempts that never address what the seeded test looks for, so pytest keeps
    # failing and the repair loop keeps asking until its policy stops it. Enough
    # turns that the *policy* is what ends the loop, not an empty script — a
    # script that runs dry would fail loudly and prove nothing about the loop.
    no_fix = json.dumps({"path": "src/login.py", "content": REAL_SEED["src/login.py"]})
    rig.llm.script(
        *[
            turn
            for index in range(1, 9)
            for turn in (
                tool_call_turn("write_file", no_fix, call_id=f"c{index}"),
                text_turn("试了一版，但没解决"),
            )
        ]
    )

    outcome = await run_task(
        plan=plan,
        project=rig.project,
        session=rig.session,
        llm=rig.llm,
        tools=rig.registry,
        vcs=rig.vcs,
        workspace=rig.workspace,
        sandbox=rig.sandbox,
        constraints=rig.constraints,
        pipeline=rig.pipeline,
        tracker=rig.tracker,
        model="replay-1",
        policy=rig.policy,
        approval=rig.approval,
        principal=rig.principal,
    )

    assert outcome.checks, 'no check was recorded'
    assert outcome.checks[-1].could_not_run is False
    assert outcome.checks[-1].passed is False, 'a failing test suite reported as passing'
    assert not outcome.delivered, 'a PR was opened for a change that does not fix anything'


async def _plan(rig):
    """The one ticket this suite uses, through the real planner.

    Shared by the pinning tests so each of them is about the *check*, not about
    ticket plumbing that `test_a_whole_task_is_decided_by_a_real_test_runner`
    already covers.
    """
    from forgepilot.orchestration import ClassifierGateway
    from forgepilot.orchestration.modes.task import plan_task
    from forgepilot.seams.classifier import TicketUnderstanding
    from forgepilot.seams.refs import TicketId
    from forgepilot.seams.tickets import Ticket, TicketPriority

    return await plan_task(
        ticket=Ticket(
            id=TicketId("T-1"),
            title="登录无限重试",
            description_md="复现步骤：\n1. 输错密码\n\n预期结果：提示密码错误\n实际结果：无限重试",
            priority=TicketPriority.HIGH,
        ),
        project=rig.project,
        constraints=rig.constraints,
        registry=rig.registry,
        classifier=ClassifierGateway(
            _FixedClassifier(
                TicketUnderstanding(category="bug", symptom="无限重试", confidence=0.95)
            )
        ),
    )


# ---------------------------------------------------------------------------
# The oracle is held fixed
# ---------------------------------------------------------------------------
#: What an agent writes when it cannot make the test pass and the test is
#: writable. This *does* pass: pytest collects it, finds no assertion, and
#: reports success.
#:
#: ``pass`` rather than a comment, and the difference is not cosmetic — a
#: function whose body is only a comment is a **SyntaxError**, so the first
#: version of this fixture made the check fail with a collection error. That made
#: the test below pass while proving nothing at all: it was asserting the check
#: failed, and the check was failing for a reason that pinning had nothing to do
#: with. Caught by disabling the pinning and finding the test still green.
WEAKENED_TEST = "def test_retry_is_bounded():\n    pass\n"


async def test_relaxing_the_test_does_not_make_the_check_pass(real_task_rig) -> None:
    """The gap this closes, and it was the largest one left in the harness.

    Until now nothing distinguished "fixed the code" from "weakened the test".
    ``run_checks`` passed either way: it runs whatever is in the tree, and the
    tree is the agent's. An agent that cannot satisfy a check has an obvious
    second option, and it is cheaper than fixing anything.

    So the project's test paths are restored to the base revision for the run.
    The agent's version is still written, still in the diff, and still needs
    reviewing — it simply does not decide the verdict.

    Note what is asserted here and what is not. The weakened file *is* in the
    tree at the end (the diff shows it), and the check still fails — which is the
    whole point. Without pinning this test would pass green and the task would be
    delivered on the strength of an assertion somebody deleted.
    """
    import json

    from forgepilot.orchestration.modes.task import run_task
    from forgepilot.providers.llm.replay import text_turn, tool_call_turn

    rig = real_task_rig
    plan = await _plan(rig)

    rig.llm.script(
        tool_call_turn(
            "write_file",
            json.dumps({"path": "tests/test_login.py", "content": WEAKENED_TEST}),
            call_id="c1",
        ),
        text_turn("断言太严了，我把它放宽了"),
        text_turn("还是过不了"),
        text_turn("放弃"),
    )

    outcome = await run_task(
        plan=plan,
        project=rig.project,
        session=rig.session,
        llm=rig.llm,
        tools=rig.registry,
        vcs=rig.vcs,
        workspace=rig.workspace,
        sandbox=rig.sandbox,
        constraints=rig.constraints,
        pipeline=rig.pipeline,
        tracker=rig.tracker,
        model="replay-1",
        policy=rig.policy,
        approval=rig.approval,
        principal=rig.principal,
    )

    final = outcome.checks[-1]
    assert final.could_not_run is False, final.reason
    assert final.passed is False, (
        "the check passed because the agent relaxed the assertion — the oracle was "
        f"not held fixed.\n{final.output}"
    )
    assert not outcome.delivered
    assert outcome.pinned_tests == ("tests/test_login.py",)
    assert "tests/test_login.py" in outcome.changed_paths, (
        "the agent's edit was dropped from the tree rather than set aside for the run"
    )
    assert final.meta["pinned"] == ["tests/test_login.py"]


async def test_the_model_is_told_why_editing_the_test_cannot_help(real_task_rig) -> None:
    """Otherwise the repair loop spends its budget on the wrong file.

    A model that reads a failure, adjusts the assertion, fails again, and is
    given no reason will keep adjusting it. The check output has to say the tests
    were restored — and it is checked here on the *recorded* event, because that
    is what reaches the model.
    """
    import json

    from forgepilot.orchestration.modes.task import run_task
    from forgepilot.providers.llm.replay import text_turn, tool_call_turn
    from forgepilot.seams.session import EventKind

    rig = real_task_rig
    plan = await _plan(rig)

    rig.llm.script(
        tool_call_turn(
            "write_file",
            json.dumps({"path": "tests/test_login.py", "content": WEAKENED_TEST}),
            call_id="c1",
        ),
        text_turn("改了测试"),
        text_turn("还是不行"),
        text_turn("放弃"),
    )

    await run_task(
        plan=plan,
        project=rig.project,
        session=rig.session,
        llm=rig.llm,
        tools=rig.registry,
        vcs=rig.vcs,
        workspace=rig.workspace,
        sandbox=rig.sandbox,
        constraints=rig.constraints,
        pipeline=rig.pipeline,
        tracker=rig.tracker,
        model="replay-1",
        policy=rig.policy,
        approval=rig.approval,
        principal=rig.principal,
    )

    checks = [e for e in rig.session.events if e.kind == EventKind.TASK_CHECK]
    assert checks, "no check reached the log"
    output = checks[-1].data["output"]
    assert "restored to the revision this task started from" in output
    assert "editing a test cannot make this pass" in output


async def test_nothing_is_pinned_when_the_agent_touched_no_tests(real_task_rig) -> None:
    """Pinning is a response to a change, not a habit.

    Restoring a path the agent never edited would be a no-op with a cost: it
    writes the file, and the report would claim the oracle was held fixed when
    there was nothing to hold.
    """
    from forgepilot.orchestration.modes.task import run_task
    from forgepilot.providers.llm.replay import text_turn

    rig = real_task_rig
    plan = await _plan(rig)
    rig.llm.script(text_turn("nothing to do"), text_turn("nothing to do"), text_turn("nothing to do"))

    outcome = await run_task(
        plan=plan,
        project=rig.project,
        session=rig.session,
        llm=rig.llm,
        tools=rig.registry,
        vcs=rig.vcs,
        workspace=rig.workspace,
        sandbox=rig.sandbox,
        constraints=rig.constraints,
        pipeline=rig.pipeline,
        tracker=rig.tracker,
        model="replay-1",
        policy=rig.policy,
        approval=rig.approval,
        principal=rig.principal,
    )

    assert outcome.pinned_tests == ()
    assert outcome.checks[-1].passed is False, "nothing was fixed, so nothing should pass"
