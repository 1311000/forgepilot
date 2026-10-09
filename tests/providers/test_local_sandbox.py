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

from forgepilot.providers.sandbox.local import LocalProcessSandboxPlugin
from forgepilot.seams.refs import SessionId
from forgepilot.seams.sandbox import (
    SandboxEnforcement,
    SandboxExecutionPolicy,
    SandboxMode,
)


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
    return await sandbox.run(
        argv,
        policy=SandboxExecutionPolicy(
            mode=SandboxMode.WORKSPACE_WRITE,
            workspace_root=tmp_path,
            session_id=SessionId("s-local"),
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
