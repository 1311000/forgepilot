"""How the ``run`` tool launches a command, and what it says when nothing ran.

Every test here comes from one real run, and each is a thing that went wrong in
it. Two are about the launch and two are about the explanation:

- the tool used its own ``shlex.split(posix=True)`` while ``run_checks`` used the
  platform-correct ``split_command``. On Windows the tool shredded
  ``C:\\…\\python.exe`` into ``C:…python.exe``, nothing was spawned, and the
  check that would have caught the model's fix never ran;
- the failure was then *attributed* to the sandbox — "records calls without
  running them" — which was false, and which is what turned a fixable command
  into a dead end. The model wrote "the sandbox doesn't actually execute" and
  stopped running anything.

The split is now shared with ``run_checks``; the launch is deliberately **not**
shared, because a model writes a shell line and a ``project.yaml`` check is a
line a human can read. Both differences are asserted below.
"""

from __future__ import annotations

import json
import os

import pytest

from forgepilot.seams.approval import ApprovalOutcome
from forgepilot.seams.refs import SessionId, TurnId
from forgepilot.seams.tools import ToolCall

WINDOWS_PATH = r"C:\tools\py\python.exe -m pytest -q"


def constraints_for(workspace):
    """The same authority `test_pipeline` builds. Spelled out rather than
    imported because a conftest is not a module you can reach from here."""
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import ExecutionConstraints, Operation, Principal

    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        permitted_operations=frozenset(
            {Operation.READ_CODE, Operation.WRITE_CODE, Operation.RUN_TESTS}
        ),
        writable_paths=("src",),
    )


def call(command: str) -> ToolCall:
    return ToolCall(
        call_id="c-run",
        name="run",
        arguments_json=json.dumps({"command": command}),
    )


async def invoke(rig, command: str):
    rig.approval.set_answerer(lambda r: ApprovalOutcome.ALLOWED_ONCE)
    return await rig.pipeline.invoke(
        call(command),
        session=rig.session,
        turn=TurnId(1),
        step=1,
        constraints=constraints_for(rig.workspace),
    )


# ---------------------------------------------------------------------------
# The splitter, which the two callers must share
# ---------------------------------------------------------------------------
def test_the_platform_decides_how_a_command_line_splits() -> None:
    """``posix=True`` treats ``\\`` as an escape.

    Which is how `C:\\…\\python.exe` became `C:…python.exe`, a path that cannot
    exist, reported to the model as a sandbox refusal.
    """
    from forgepilot.runtime.commandline import split_command

    parts = split_command(WINDOWS_PATH)

    if os.name == "nt":
        assert parts[0] == r"C:\tools\py\python.exe"
    assert parts[1:] == ["-m", "pytest", "-q"]


async def test_run_checks_and_the_run_tool_split_the_same_way() -> None:
    """The actual defect was not the rule, it was that there were two of it.

    ``run_checks`` had the correct splitter and the tool had its own. Asserting
    that the two agree is what stops the next correction from being made at one
    call site again.
    """
    from forgepilot.runtime.commandline import split_command
    from forgepilot.runtime.repair import split_command as checks_splitter

    assert checks_splitter is split_command


# ---------------------------------------------------------------------------
# The launch: a shell for the model, and only for the model
# ---------------------------------------------------------------------------
async def test_the_tool_hands_the_sandbox_a_shell_invocation(rig) -> None:
    """Asserted against the *recording* sandbox's record, so it is the argv that
    actually crossed the boundary rather than one recomputed in the test."""
    from forgepilot.runtime.commandline import shell_argv

    await invoke(rig, "pytest -q 2>&1 | tail -30")

    assert rig.sandbox.calls, "the command never reached the sandbox"
    assert rig.sandbox.calls[-1].argv == shell_argv("pytest -q 2>&1 | tail -30")


async def test_a_model_command_really_goes_through_a_shell(tmp_path) -> None:
    """The behaviour, not the argv shape.

    ``a && b`` is what a model writes and what an argv-only launch cannot do: it
    ran as ``echo`` with two odd arguments. This runs it for real, through the
    provider that actually spawns processes.
    """
    from forgepilot.providers.sandbox.local import LocalProcessSandboxPlugin
    from forgepilot.runtime.commandline import shell_argv
    from forgepilot.runtime.sandbox_env import command_env
    from forgepilot.seams.sandbox import SandboxExecutionPolicy, SandboxMode
    from forgepilot.seams.security import NetworkAccess

    plugin = LocalProcessSandboxPlugin.__new__(LocalProcessSandboxPlugin)
    plugin.max_output_bytes = 64_000

    result = await plugin.run(
        shell_argv("echo first && echo second"),
        policy=SandboxExecutionPolicy(
            mode=SandboxMode.WORKSPACE_WRITE,
            workspace_root=tmp_path,
            session_id=SessionId("shell-probe"),
            # The backend provides no containment, so it refuses a policy that asks
            # it to restrict something. Reaching its execution path at all means
            # having accepted an uncontained command.
            network=NetworkAccess.ALLOW,
        ),
        cwd=tmp_path,
        env=command_env(),
        timeout_s=30,
    )

    assert result.executed
    assert result.exit_code == 0
    assert "first" in result.stdout and "second" in result.stdout, (
        "only one command ran, so this is still an argv list rather than a shell line"
    )


def test_the_checks_do_not_get_a_shell() -> None:
    """The asymmetry, stated where it could be broken by accident.

    A ``project.yaml`` check is a line a human wrote and can read. Giving it a
    shell would make `&&` in it a *feature*, and the pipeline has been careful
    never to hand a configured command line a shell — that is what makes such a
    `&&` a mistake somebody can see.
    """
    import inspect

    from forgepilot.runtime import repair

    source = inspect.getsource(repair)
    assert "shell_argv" not in source, (
        "run_checks gained a shell; that changes what a project.yaml `test:` line "
        "means, and it is a decision rather than a refactor"
    )


# ---------------------------------------------------------------------------
# Telling the model the truth about *why* nothing ran
# ---------------------------------------------------------------------------
def test_a_missing_executable_is_blamed_on_the_command() -> None:
    """`executed=False` is not evidence that the sandbox refused.

    A command whose executable does not exist is equally "not executed", and the
    two need opposite responses: fix the command, versus stop expecting execution
    to work at all.
    """
    from forgepilot.runtime.sandbox_denial import not_executed_reason
    from forgepilot.seams.sandbox import ExecResult, SandboxEnforcement

    result = ExecResult(
        argv=("C:somewherepython.exe", "-m", "pytest"),
        exit_code=127,
        stderr="command not found: 'C:somewherepython.exe'",
        enforcement=SandboxEnforcement.NONE,
        executed=False,
    )

    reason = not_executed_reason(result)

    assert "could not be found" in reason
    assert "not with the sandbox" in reason
    assert "may not execute commands at all" not in reason


def test_a_recording_sandbox_is_not_reported_as_a_bad_command() -> None:
    """The other direction, which is the one the old message always took."""
    from forgepilot.runtime.sandbox_denial import not_executed_reason
    from forgepilot.seams.sandbox import ExecResult, SandboxEnforcement

    result = ExecResult(
        argv=("pytest", "-q"),
        exit_code=0,
        enforcement=SandboxEnforcement.NONE,
        executed=False,
    )

    reason = not_executed_reason(result)

    assert "may not execute commands at all" in reason
    assert "could not be found" not in reason


async def test_an_unexecuted_command_names_which_of_the_two_it_was(rig) -> None:
    """And the tool actually asks. A pure function nobody calls proves nothing."""
    rig.sandbox._exit_code = 127  # the recording sandbox's only knob for this

    outcome = await invoke(rig, "definitely-not-a-real-binary")

    assert outcome.meta["executed"] is False
    assert "could not be found" in outcome.content


@pytest.mark.parametrize("command", ["", "   "])
async def test_an_empty_command_is_refused_before_a_shell_is_started(rig, command: str) -> None:
    """`sh -c ""` exits 0 and prints nothing, which reads as success."""
    outcome = await invoke(rig, command)

    assert outcome.is_error
    assert "empty command" in outcome.content
    assert rig.sandbox.calls == [], "nothing should have been spawned at all"
