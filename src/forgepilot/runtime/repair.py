"""Running a project's checks, and the repair loop built on them.

The harness runs the project's build/test commands — that *is* the "fixed
workflow" the design calls for. Two things about how it does so are deliberate:

**It goes through the sandbox, and checks the operation first.** A command the
harness runs is still a command, so it gets the same containment as one the model
asked for. Skipping that "because it is our own code" is precisely the reasoning
that produces an escape hatch.

**The result is recorded as its own event kind, not as a tool result.** The model
must see why a check failed, but it never *asked* for the check — and a
``tool/result`` with no preceding assistant tool call is a malformed conversation
that endpoints reject. ``task/check`` is structured, model-visible, and renders as
a user message (see ``runtime/projection.py``).

The repair loop itself lives with the task runner. It is bounded by the project's
``retry`` policy rather than looping until it works: an unbounded repair loop is
how a broken build consumes a day.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from forgepilot.runtime.sandbox_env import command_env
from forgepilot.seams.refs import SessionId, TurnId
from forgepilot.seams.sandbox import (
    ExecResult,
    SandboxEnforcement,
    SandboxExecutionPolicy,
    SandboxMode,
)
from forgepilot.seams.security import ExecutionConstraints, Operation
from forgepilot.seams.session import EventKind, SurfaceOp

__all__ = ["CheckResult", "run_checks", "split_command"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What a check run concluded."""

    passed: bool
    commands: tuple[str, ...] = ()
    command: str = ""
    output: str = ""
    exit_code: int = 0
    #: True when the check could not run at all — a missing permission, an
    #: unexecuting sandbox, no commands configured. Distinguished from a failure
    #: because the two need different responses: one is a configuration problem,
    #: the other is a bug in the code under test.
    could_not_run: bool = False
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def inconclusive(self) -> bool:
        return self.could_not_run


async def run_checks(
    *,
    commands: Sequence[str],
    sandbox: Any,
    session: Any,
    constraints: ExecutionConstraints,
    workspace_root: Path | None,
    session_id: SessionId,
    turn: TurnId | None = None,
    step: int | None = None,
    timeout_s: float = 900.0,
) -> CheckResult:
    """Run the project's check commands as one chained command."""
    if not commands:
        return CheckResult(
            passed=True,
            output="no checks are configured for this project",
            could_not_run=True,
            reason="no checks configured",
            meta={"skipped": True},
        )

    command = " && ".join(commands)

    if not constraints.allows_operation(Operation.RUN_TESTS):
        # Refused rather than skipped. Reporting a pass for checks nobody may run
        # would claim the code was verified when nothing looked at it.
        result = CheckResult(
            passed=False,
            commands=tuple(commands),
            command=command,
            output=(
                "the caller may not run tests, so the project's checks were not run. "
                "This is not a pass."
            ),
            could_not_run=True,
            reason="run_tests is not permitted",
        )
        await _record(session, result, turn, step)
        return result

    # Each configured command is its own process, in order, stopping at the first
    # failure. They used to be joined with " && " and handed to `exec` **without a
    # shell**, so the operator became a literal argv element —
    # `["pytest", "-q", "&&", "mypy", "src"]` — and everything after the first
    # command was passed to it as arguments. Nothing noticed while the only
    # sandboxes in the tree answered from a script and never looked at argv.
    #
    # Chaining in Python rather than in a shell is deliberate: a shell here would
    # mean project configuration reached `sh -c`, and the pipeline has been
    # careful never to give a command line a shell to be interpreted by.
    broken: CheckResult | None = None
    exit_code = 0
    timed_out = False
    enforcement = SandboxEnforcement.NONE
    sections: list[str] = []

    for index, one in enumerate(commands, start=1):
        argv = split_command(one)
        if not argv:
            broken = CheckResult(
                passed=False,
                commands=tuple(commands),
                command=command,
                output=f"check {index} is empty: {one!r}",
                could_not_run=True,
                reason="empty command",
            )
            break

        outcome = await sandbox.run(
            argv,
            policy=SandboxExecutionPolicy(
                mode=SandboxMode.WORKSPACE_WRITE,
                workspace_root=workspace_root or Path("."),
                session_id=session_id,
                network=constraints.network,
            ),
            cwd=workspace_root or Path("."),
            env=command_env(),
            timeout_s=timeout_s,
        )

        if not getattr(outcome, "executed", True):
            # A sandbox that answers without executing cannot verify anything, and
            # saying otherwise would be the exact lie the recording provider was
            # fixed to stop telling.
            broken = CheckResult(
                passed=False,
                commands=tuple(commands),
                command=one,
                output=(
                    f"the configured sandbox ({outcome.enforcement.value!r}) does not execute "
                    f"commands, so the checks did not run and their result is unknown."
                ),
                could_not_run=True,
                reason="sandbox does not execute",
            )
            break

        enforcement = outcome.enforcement
        timed_out = outcome.timed_out
        exit_code = outcome.exit_code
        sections.append(_section(index, len(commands), one, outcome))
        if exit_code != 0:
            break

    if broken is None:
        result = CheckResult(
            passed=exit_code == 0,
            commands=tuple(commands),
            command=command,
            output="\n".join(sections).strip(),
            exit_code=exit_code,
            meta={
                "enforcement": str(enforcement),
                "timed_out": timed_out,
                # Which command stopped the chain, when one did.
                "failedIndex": next(
                    (i for i, s in enumerate(sections, start=1) if i == len(sections) and exit_code != 0),
                    None,
                ),
            },
        )
    else:
        result = broken

    await _record(session, result, turn, step)
    return result


def _section(index: int, total: int, command: str, outcome: Any) -> str:
    """One command's output, labelled.

    Labelled only when there is more than one, so the common single-command case
    stays as clean as it was — but a multi-command failure says *which* command
    failed, which is the whole point of running them separately.
    """
    body = (outcome.stdout or "") + (("\n" + outcome.stderr) if outcome.stderr else "")
    body = body.strip()
    if total == 1:
        return body
    return f"$ {command}\n{body}"


def split_command(command: str) -> list[str]:
    """Split a configured command line the way *this platform* defines one.

    ``shlex.split(posix=True)`` treats a backslash as an escape, so on Windows an
    absolute path loses its separators — ``C:\\…\\python.exe`` becomes
    ``C:…python.exe`` — the sandbox reports "command not found", and the check
    reads as a configuration problem rather than as the harness mangling its own
    argv. Production is Linux, where either form works; the dev machine is not,
    and a check that cannot run where you develop is a loop that never closes
    there.

    Non-posix mode preserves the path but keeps one layer of quoting, so that is
    removed. The result is the same argv on both platforms for the same command
    line, which is what "the same code" should mean.
    """
    if os.name == "nt":
        parts = shlex.split(command, posix=False)
        return [
            part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part
            for part in parts
        ]
    return shlex.split(command, posix=True)


async def _record(
    session: Any, result: CheckResult, turn: TurnId | None, step: int | None
) -> None:
    """Record the check so the model can see it and an auditor can reconstruct it."""
    if session is None:
        return
    await session.append(
        EventKind.TASK_CHECK,
        {
            "passed": result.passed,
            "command": result.command,
            "commands": list(result.commands),
            "exitCode": result.exit_code,
            "output": result.output,
            "couldNotRun": result.could_not_run,
            "reason": result.reason,
            "meta": dict(result.meta),
        },
        surface=SurfaceOp.APPEND,
        turn=turn,
        step=step,
    )
