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

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from forgepilot.seams.refs import SessionId, TurnId
from forgepilot.seams.sandbox import SandboxExecutionPolicy, SandboxMode
from forgepilot.seams.security import ExecutionConstraints, Operation
from forgepilot.seams.session import EventKind, SurfaceOp

__all__ = ["CheckResult", "run_checks"]


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

    argv = shlex.split(command, posix=True)
    if not argv:
        return CheckResult(
            passed=False, commands=tuple(commands), could_not_run=True, reason="empty command"
        )

    outcome = await sandbox.run(
        argv,
        policy=SandboxExecutionPolicy(
            mode=SandboxMode.WORKSPACE_WRITE,
            workspace_root=workspace_root or Path("."),
            session_id=session_id,
            network=constraints.network,
        ),
        cwd=workspace_root or Path("."),
        env={},
        timeout_s=timeout_s,
    )

    if not getattr(outcome, "executed", True):
        # A sandbox that answers without executing cannot verify anything, and
        # saying otherwise would be the exact lie the recording provider was
        # fixed to stop telling.
        result = CheckResult(
            passed=False,
            commands=tuple(commands),
            command=command,
            output=(
                f"the configured sandbox ({outcome.enforcement.value!r}) does not execute "
                f"commands, so the checks did not run and their result is unknown."
            ),
            could_not_run=True,
            reason="sandbox does not execute",
        )
        await _record(session, result, turn, step)
        return result

    output = (outcome.stdout or "") + (
        ("\n" + outcome.stderr) if outcome.stderr else ""
    )
    result = CheckResult(
        passed=outcome.exit_code == 0,
        commands=tuple(commands),
        command=command,
        output=output.strip(),
        exit_code=outcome.exit_code,
        meta={"enforcement": str(outcome.enforcement), "timed_out": outcome.timed_out},
    )
    await _record(session, result, turn, step)
    return result


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
