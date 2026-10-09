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
from pathlib import Path

import pytest

from forgepilot.runtime.repair import run_checks
from forgepilot.seams.refs import PrincipalId, SessionId, TenantId, TurnId
from forgepilot.seams.security import ExecutionConstraints, Operation, Principal


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
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        permitted_operations=frozenset({Operation.RUN_TESTS}),
        writable_paths=("src",),
    )


@pytest.fixture
async def session():
    """A live session, because run_checks records a ``task/check`` event."""
    from datetime import datetime, timezone

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
        SessionHeader(session_id=sid, created_at=datetime.now(timezone.utc), tenant="acme")
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
