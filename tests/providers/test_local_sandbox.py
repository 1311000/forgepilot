"""The local-execution sandbox: it really runs commands, and says how weakly.

This backend is what makes the repair loop close on a development machine. Until
it existed, every `task/check` was `could_not_run`, so the harness could edit
files but never establish that the edit was right — the loop looked like it
worked only because the tests fed it a scripted exit code.

It provides **no containment**, and every test here is arranged around that
being stated rather than papered over. `probe()` reports `NONE`, `arm` therefore
refuses `exec` unless an operator explicitly lowers the requirement, and the
execution it *does* perform reports its own weakness in `ExecResult.enforcement`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from forgepilot.providers.sandbox.local import LocalProcessSandboxPlugin, PolicyNotEnforceable
from forgepilot.seams.refs import SessionId
from forgepilot.seams.sandbox import (
    SandboxEnforcement,
    SandboxExecutionPolicy,
    SandboxMode,
)
from forgepilot.seams.security import NetworkAccess


@pytest.fixture
async def sandbox():
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.sandbox.local:LocalProcessSandboxPlugin"), {}
    )
    try:
        yield root.sandbox
    finally:
        await dispose_fiber(fiber)


@pytest.fixture
def policy(tmp_path: Path) -> SandboxExecutionPolicy:
    return SandboxExecutionPolicy(
        mode=SandboxMode.WORKSPACE_WRITE,
        workspace_root=tmp_path,
        session_id=SessionId("s-local"),
    )


async def run(sandbox, argv, tmp_path, *, timeout_s: float = 10.0, stdin=None):
    """Run through the backend the way a deployment that accepted it would.

    ``network=ALLOW`` is not a convenience — it is the *declaration* this backend
    now requires. It provides no containment, so it refuses a policy that asks it
    to restrict something (see the network tests below); a caller that wants to
    execute here has to say out loud that it accepts an uncontained command. The
    alternative to spelling it here would be a helper that quietly hands every test
    a policy the backend is entitled to refuse.
    """
    return await sandbox.run(
        argv,
        policy=SandboxExecutionPolicy(
            mode=SandboxMode.WORKSPACE_WRITE,
            workspace_root=tmp_path,
            session_id=SessionId("s-local"),
            network=NetworkAccess.ALLOW,
        ),
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"},
        timeout_s=timeout_s,
        stdin=stdin,
    )


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


# ---------------------------------------------------------------------------
# It executes
# ---------------------------------------------------------------------------
async def test_a_command_really_runs_and_its_output_comes_back(sandbox, tmp_path) -> None:
    """The assertion the whole loop rests on: the bytes are the command's own."""
    result = await run(sandbox, python("print('hello from a real process')"), tmp_path)

    assert result.executed is True
    assert result.exit_code == 0
    assert "hello from a real process" in result.stdout


async def test_a_failing_command_reports_its_own_exit_code(sandbox, tmp_path) -> None:
    """A check that fails must fail. Reporting 0 for everything is how a red
    suite reads as a green one."""
    result = await run(sandbox, python("import sys; sys.exit(3)"), tmp_path)

    assert result.executed is True
    assert result.exit_code == 3


async def test_stderr_is_kept_separate_from_stdout(sandbox, tmp_path) -> None:
    """A traceback and a print are not the same evidence, and a check that
    merges them makes a failing test look like passing output."""
    result = await run(
        sandbox,
        python("import sys; print('out'); print('err', file=sys.stderr)"),
        tmp_path,
    )

    assert "out" in result.stdout and "err" not in result.stdout
    assert "err" in result.stderr and "out" not in result.stderr


async def test_the_command_runs_in_the_workspace_it_was_given(sandbox, tmp_path) -> None:
    """`cwd` is the boundary the harness can actually hold, so it must hold."""
    (tmp_path / "marker.txt").write_text("here\n", encoding="utf-8")

    result = await run(sandbox, python("print(open('marker.txt').read().strip())"), tmp_path)

    assert "here" in result.stdout


# ---------------------------------------------------------------------------
# It says how weakly it enforces
# ---------------------------------------------------------------------------
async def test_it_reports_no_enforcement_and_will_not_be_talked_out_of_it(
    sandbox, tmp_path
) -> None:
    """A backend must not be configurable into claiming containment it lacks.

    `probe()` is documented as "a fact about the machine, not a preference". A
    config knob that let an operator set `enforcement: full` here would turn the
    one honest signal in the sandbox stack into a formality — and `arm`'s refusal
    to run `exec` below `full` is built on it.
    """
    assert sandbox.probe() is SandboxEnforcement.NONE


async def test_each_result_reports_the_enforcement_actually_applied(sandbox, tmp_path) -> None:
    """Not the policy's request — what this execution really had."""
    result = await run(sandbox, python("pass"), tmp_path)

    assert result.enforcement is SandboxEnforcement.NONE


async def test_a_read_only_policy_is_refused_rather_than_ignored(sandbox, tmp_path) -> None:
    """A backend that cannot restrict must not accept a policy that asks it to.

    ``mode`` was accepted and never read, so a caller passing ``READ_ONLY`` got a
    command that could write anywhere. Nothing did that yet, which is why it was
    harmless — and why it would have stayed harmless right up until someone used
    it for something that mattered.

    ``WORKSPACE_WRITE`` is accepted because this backend's honest description is
    "runs the command, restricts nothing": that is at least *not less* than the
    caller asked for, and ``enforcement=NONE`` on every result says so. Read-only
    asks for the opposite of what happens.
    """
    with pytest.raises(PolicyNotEnforceable, match="read-only"):
        await sandbox.run(
            python("pass"),
            policy=SandboxExecutionPolicy(
                mode=SandboxMode.READ_ONLY,
                workspace_root=tmp_path,
                session_id=SessionId("s-local"),
            ),
            cwd=tmp_path,
            env={},
            timeout_s=5.0,
        )


async def test_a_network_deny_policy_is_refused_rather_than_ignored(sandbox, tmp_path) -> None:
    """The field that was *still* accepted-and-ignored after ``mode`` was fixed.

    ``network`` defaults to ``DENY``, every caller threads its constraints through
    it, and this backend never read it — so a command ran with full network access
    while the model's own scope section said ``Network: deny``. **A boundary that is
    asserted to the model and not applied is worse than no boundary**, because the
    model reasons from it: it will not try the forbidden route and will not report
    one either. The prompt is not a place a lie can sit harmlessly.

    And the difference from ``WORKSPACE_WRITE``, which *is* accepted, is the point
    rather than an inconsistency: "runs the command, restricts nothing" is not less
    than "may write in the workspace", so running it is honest. A requested ``DENY``
    that is not applied is the opposite of what was asked.
    """
    with pytest.raises(PolicyNotEnforceable, match="network"):
        await sandbox.run(
            python("pass"),
            policy=SandboxExecutionPolicy(
                mode=SandboxMode.WORKSPACE_WRITE,
                workspace_root=tmp_path,
                session_id=SessionId("s-local"),
                network=NetworkAccess.DENY,
            ),
            cwd=tmp_path,
            env={},
            timeout_s=5.0,
        )


async def test_nothing_runs_when_the_network_policy_is_refused(sandbox, tmp_path) -> None:
    """Refused *before* the spawn, so the effect never happens.

    A command that must not reach the network is refused precisely *because* this
    backend would let it. Refusing afterwards would be worse than not refusing: the
    effect would already have happened.
    """
    marker = tmp_path / "should-not-exist.txt"

    with pytest.raises(PolicyNotEnforceable):
        await sandbox.run(
            python(f"open({str(marker)!r}, 'w').write('ran')"),
            policy=SandboxExecutionPolicy(
                mode=SandboxMode.WORKSPACE_WRITE,
                workspace_root=tmp_path,
                session_id=SessionId("s-local"),
                network=NetworkAccess.DENY,
            ),
            cwd=tmp_path,
            env={},
            timeout_s=5.0,
        )

    assert not marker.exists()


async def test_nothing_was_spawned_when_a_policy_is_refused(sandbox, tmp_path) -> None:
    """Refusing after running would be worse than not refusing at all."""
    marker = tmp_path / "should-not-exist.txt"

    with pytest.raises(PolicyNotEnforceable):
        await sandbox.run(
            python(f"open({str(marker)!r}, 'w').write('ran')"),
            policy=SandboxExecutionPolicy(
                mode=SandboxMode.READ_ONLY,
                workspace_root=tmp_path,
                session_id=SessionId("s-local"),
            ),
            cwd=tmp_path,
            env={},
            timeout_s=5.0,
        )

    assert not marker.exists()


# ---------------------------------------------------------------------------
# It can be stopped
# ---------------------------------------------------------------------------
async def test_a_hung_command_is_killed_and_reported_as_timed_out(sandbox, tmp_path) -> None:
    """'It hung' and 'it passed' must never be the same result."""
    result = await run(
        sandbox, python("import time; time.sleep(30)"), tmp_path, timeout_s=0.5
    )

    assert result.timed_out is True
    assert result.executed is True, "it did run — it just did not finish"
    assert result.exit_code != 0


async def test_a_missing_binary_is_reported_rather_than_raised(sandbox, tmp_path) -> None:
    """A typo in `project.yaml` should read as a failed check with a reason, not
    as a crash that takes the turn down with it."""
    result = await run(sandbox, ["definitely-not-a-real-binary-xyz"], tmp_path)

    assert result.executed is False, "nothing was spawned, so nothing ran"
    assert result.exit_code != 0
    assert "definitely-not-a-real-binary-xyz" in result.stderr
