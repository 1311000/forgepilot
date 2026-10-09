"""How the ``run`` tool turns a command into an ``argv``, and what it says when
nothing happened.

Every test here comes from one real run, and each one is a thing that went wrong
in it. The model asked to run a Windows-path interpreter, the tool shredded the
path into ``C:UsersAdministrator...``, nothing was spawned, and the tool
explained the failure as *the sandbox records calls without running them*. The
model believed it, wrote "the sandbox doesn't actually execute", and stopped
trying to run anything — including the tests that would have told it whether its
fix worked.

So there are two defects under test, and the second is the more dangerous:

- the argv was mangled by a splitter that disagrees with the one ``run_checks``
  already used correctly;
- the failure was *attributed* to the wrong party, which is what turned a
  fixable command into a dead end.
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
    """The same authority `test_pipeline` builds. Duplicated rather than imported
    because a conftest is not a module you can reach from here, and every test in
    this suite already spells it out."""
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
# The argv
# ---------------------------------------------------------------------------
def test_the_platform_decides_how_a_command_line_splits() -> None:
    """Both callers must split identically, or the same line means two things.

    ``run_checks`` was fixed for this first; the tool was not, which is how a
    single repository came to contain both the correct rule and a caller that
    ignored it.
    """
    from forgepilot.runtime.commandline import split_command

    parts = split_command(WINDOWS_PATH)

    if os.name == "nt":
        # The bug: `posix=True` treats `\` as an escape, so this arrived as
        # `C:toolspypython.exe` — a path that cannot exist, reported to the model
        # as a sandbox refusal.
        assert parts[0] == r"C:\tools\py\python.exe"
    else:
        assert parts[0] == "C:toolspypython.exe" or parts[0] == WINDOWS_PATH.split()[0]
    assert parts[1:] == ["-m", "pytest", "-q"]


async def test_the_tool_hands_the_sandbox_the_argv_this_platform_would_build(rig) -> None:
    """Asserted against the *recording* sandbox's record, so it is the argv that
    actually crossed the boundary rather than one recomputed in the test."""
    from forgepilot.runtime.commandline import split_command

    await invoke(rig, WINDOWS_PATH)

    assert rig.sandbox.calls, "the command never reached the sandbox"
    assert rig.sandbox.calls[-1].argv == tuple(split_command(WINDOWS_PATH))


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


# ---------------------------------------------------------------------------
# Shell syntax, which nothing here interprets
# ---------------------------------------------------------------------------
async def test_shell_syntax_is_called_out_as_a_literal_argument(rig) -> None:
    """`ls -la && find .` runs as `ls` with five odd arguments.

    It really did happen, and the answer was `ls: unknown option -- e` — an
    error that names `ls` and says nothing about the `&&` that caused it. There
    is no shell here to reject the syntax, so the honest thing is to report what
    the operator became.
    """
    outcome = await invoke(rig, 'ls -la && echo "x" | head -3')

    assert "not through a shell" in outcome.content
    assert "'&&'" in outcome.content


@pytest.mark.parametrize("command", ["pytest -q", "git status", "python -m pytest"])
async def test_a_plain_command_gets_no_shell_lecture(rig, command: str) -> None:
    """A hint that appears on every result is a hint nobody reads."""
    outcome = await invoke(rig, command)

    assert "not through a shell" not in outcome.content


def test_a_redirect_operator_is_recognised() -> None:
    """`>` is read redirection far more often than it is a stray character."""
    from forgepilot.runtime.commandline import looks_like_shell, shell_hint

    assert looks_like_shell("pytest -q > out.txt") == (">", "redirect the output to a file")
    assert looks_like_shell("pytest -q") is None
    assert shell_hint("pytest -q") == ""
