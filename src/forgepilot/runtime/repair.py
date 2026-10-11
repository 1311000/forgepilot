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

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgepilot.runtime.commandline import split_command
from forgepilot.runtime.pinning import unpinned_notice
from forgepilot.runtime.sandbox_env import command_env
from forgepilot.seams.refs import SessionId, TurnId
from forgepilot.seams.sandbox import (
    SandboxEnforcement,
    SandboxExecutionPolicy,
    SandboxMode,
)
from forgepilot.seams.security import ExecutionConstraints, Operation
from forgepilot.seams.session import EventKind, SurfaceOp

# `split_command` moved to `runtime/commandline.py` when the `run` tool needed
# it too. It was never about repair; it is a fact about the platform, and two
# callers that split differently is how the same command line comes to mean two
# things.
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
    pinned: Sequence[str] = (),
) -> CheckResult:
    """Run the project's check commands, in order, stopping at the first failure.

    ``pinned`` names the test paths that were restored to the base revision for
    this run — see :mod:`forgepilot.runtime.pinning`. It changes nothing about
    how the commands run; it is carried so the model is told, because a repair
    loop that edits a test, fails again, and cannot find out why is a loop that
    spends its whole budget on the wrong file.
    """
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
        body = "\n".join(sections).strip()
        # Prepended rather than appended: on a failure the tail is the traceback,
        # and a note after it is a note nobody reads — including the model, which
        # is the one that has to act on it.
        notice = unpinned_notice(pinned)
        result = CheckResult(
            passed=exit_code == 0,
            commands=tuple(commands),
            command=command,
            output=f"{notice}\n{body}".strip() if notice else body,
            exit_code=exit_code,
            meta={
                "pinned": list(pinned),
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


def combine_checks(named: Sequence[tuple[str, CheckResult]]) -> CheckResult:
    """One verdict from the same checks run over two different trees.

    **Why two runs.** Pinning the project's tests to the base revision is what
    stops an agent satisfying a check by relaxing it — but it makes a *feature*
    unverifiable, and that is not a corner case: when the thing being asked for
    does not exist yet, the new test **is** the specification, and restoring the
    test file to base deletes it. The pinned run then passes on tests that have
    never heard of the feature, and the task reports success with the feature
    absent. Measured, not theorised: `evals/tasks/feature/task.yaml`'s `cheating`
    variant writes the test and not the implementation, and it scored a pass.

    So both trees are checked, and **both have to pass**:

    - **pinned** — the tests as they were. Proves nothing was weakened.
    - **agent** — the tests as the agent left them. Proves the feature its own
      tests describe actually exists.

    Neither alone is enough. Pinned alone cannot see a feature; agent alone cannot
    see a weakened assertion. The conjunction sees both, and the cost is one extra
    run of the project's own command.
    """
    passed = all(check.passed for _, check in named)
    ran = [check for _, check in named if not check.could_not_run]
    # `could_not_run` only when *nothing* ran: if one tree was checked, its answer
    # is evidence, and reporting "could not run" over it would discard the answer
    # that exists.
    could_not_run = not ran
    reason = ""
    if could_not_run:
        reason = next((c.reason for _, c in named if c.reason), "checks could not run")
    elif not passed:
        failing = [label for label, check in named if not check.passed]
        reason = "the checks failed against: " + ", ".join(failing)

    sections: list[str] = []
    for label, check in named:
        verdict = "did-not-run" if check.could_not_run else ("passed" if check.passed else "FAILED")
        sections.append(f"### checks against the {label} tree — {verdict}\n{check.output}".strip())

    first = named[0][1]
    return CheckResult(
        passed=passed,
        commands=first.commands,
        command=first.command,
        output="\n\n".join(sections),
        exit_code=0 if passed else next(
            (c.exit_code for _, c in named if not c.passed and c.exit_code), 1
        ),
        could_not_run=could_not_run,
        reason=reason,
        meta={
            "runs": {
                label: {"passed": c.passed, "couldNotRun": c.could_not_run, "exitCode": c.exit_code}
                for label, c in named
            },
            "pinned": [p for _, c in named for p in c.meta.get("pinned", [])],
        },
    )
