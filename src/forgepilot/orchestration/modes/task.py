"""Task mode planning: ticket in, an executable plan out.

**This stops before execution.** Producing the plan is M7's job; running it is
M8's. Keeping them apart is not ceremony — it is what lets the two invariants be
asserted without a model in the loop, and it makes the plan itself inspectable:
what adapters were chosen, what the model would be told, and which tools were
withheld and why.

The order of the steps is the security property:

1. **Rules first.** A ticket the deterministic preprocessor can reject never
   reaches the classifier, so an incomplete report costs zero model calls.
2. **Classify only what survived.** And only to *understand*, never to decide.
3. **Route from configuration.** The classification picks a *name*; the project
   decides what that name means; the constraints decide what survives.
4. **Assemble context with the trusted/untrusted split.** Ticket-derived text
   goes in the user turn, never the system prompt.

Every early return goes to ``awaiting_info`` — asking a human. There is no path
that guesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from forgepilot.config.project import ProjectConfig
from forgepilot.orchestration.classifier import ClassifierGateway
from forgepilot.runtime.loop.turn import DEFAULT_MAX_STEPS
from forgepilot.orchestration.context_assembly import (
    PromptSection,
    assemble_system_sections,
    compose_task_brief,
)
from forgepilot.orchestration.preprocess import PreprocessResult, preprocess
from forgepilot.orchestration.router import AdapterSelection, select_adapters
from forgepilot.orchestration.task_state import TaskState, TaskStateMachine
from forgepilot.seams.classifier import ClassificationRequest, TicketUnderstanding
from forgepilot.seams.memory import MemoryRecord
from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.tickets import Ticket

__all__ = ["TaskPlan", "plan_task"]


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """What this task would run with, or why it cannot yet run at all."""

    state: TaskState
    ticket: Ticket
    preprocessed: PreprocessResult
    reason: str = ""
    #: Which repositories the work targets. Resolved deterministically: what the
    #: ticket names, or the project's single repository.
    repos: tuple[Any, ...] = ()
    understanding: TicketUnderstanding | None = None
    selection: AdapterSelection | None = None
    system_sections: tuple[PromptSection, ...] = ()
    brief: str = ""
    transitions: tuple[tuple[TaskState, str], ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return self.state is TaskState.QUEUED

    @property
    def needs_human(self) -> bool:
        return self.state is TaskState.AWAITING_INFO

    @property
    def system_texts(self) -> tuple[str, ...]:
        return tuple(section.render() for section in self.system_sections)

    def tool_schemas(self, registry: Any) -> tuple[Any, ...]:
        """Schemas for the selected tools only.

        This is where routing stops being decorative: the loop is handed *only*
        these, so a tool the router withheld is not merely refused later — the
        model never sees it.
        """
        if self.selection is None:
            return ()
        return tuple(
            schema
            for schema in registry.schemas()
            if schema.name in self.selection.tools
        )

    def describe(self) -> str:
        """A short human-readable summary. Used by tests and by the CLI."""
        if self.needs_human:
            return f"awaiting human input: {self.reason}"
        assert self.selection is not None  # noqa: S101 - invariant of `ready`
        withheld = sorted(self.selection.withheld)
        unknown = sorted(self.selection.unknown)
        line = (
            f"ready: tool set {self.selection.tool_set!r}, engine "
            f"{self.selection.engine!r}, {len(self.selection.tools)} tool(s)"
        )
        if withheld:
            line += f"; withheld by constraints: {withheld}"
        if unknown:
            line += f"; not in registry: {unknown}"
        return line


async def plan_task(
    *,
    ticket: Ticket,
    project: ProjectConfig,
    constraints: ExecutionConstraints,
    registry: Any,
    classifier: ClassifierGateway,
    workspace_root: Path | None = None,
    memories: Sequence[MemoryRecord] = (),
) -> TaskPlan:
    """Preprocess, classify if needed, route, and assemble. Executes nothing."""
    machine = TaskStateMachine()

    preprocessed = preprocess(ticket, known_repos=project.repos)

    if preprocessed.needs_human:
        # The whole point: an incomplete ticket is rejected by rules alone, so
        # the classifier is never consulted.
        machine.to(TaskState.AWAITING_INFO, preprocessed.reason)
        return TaskPlan(
            state=TaskState.AWAITING_INFO,
            ticket=ticket,
            preprocessed=preprocessed,
            reason=preprocessed.reason,
            transitions=tuple(machine.history),
        )

    # Which repository? Answered by rules, and answered *before* the classifier
    # for the same reason as above. A ticket naming a repository nobody knows
    # cannot be scoped, and asking a model to guess one would be asking it to
    # choose where to write.
    target_repos = preprocessed.repos or tuple(project.repos)
    if not target_repos:
        reason = (
            "no repository could be determined for this ticket: it names none the "
            "project knows about, and the project declares none"
        )
        machine.to(TaskState.AWAITING_INFO, reason)
        return TaskPlan(
            state=TaskState.AWAITING_INFO,
            ticket=ticket,
            preprocessed=preprocessed,
            reason=reason,
            transitions=tuple(machine.history),
        )

    understanding = await classifier.classify(
        ClassificationRequest(
            ticket_id=ticket.id,
            title=ticket.title,
            description=ticket.description_md,
            comments=tuple(c.body_md for c in ticket.comments),
            signals=dict(preprocessed.signals),
        )
    )

    if understanding.needs_human:
        # A classifier that is unsure, or that noticed something missing, sends
        # the task to a person. It never fills the gap with a guess.
        reason = (
            f"classifier confidence {understanding.confidence:.2f}"
            + (f"; could not determine: {', '.join(understanding.missing)}" if understanding.missing else "")
        )
        machine.to(TaskState.AWAITING_INFO, reason)
        return TaskPlan(
            state=TaskState.AWAITING_INFO,
            ticket=ticket,
            preprocessed=preprocessed,
            understanding=understanding,
            reason=reason,
            transitions=tuple(machine.history),
        )

    selection = select_adapters(
        understanding=understanding,
        project=project,
        constraints=constraints,
        registry=registry,
    )
    sections = assemble_system_sections(
        project=project, constraints=constraints, workspace_root=workspace_root
    )
    brief = compose_task_brief(
        ticket=ticket,
        understanding=understanding,
        preprocessed=preprocessed,
        memories=memories,
    )

    return TaskPlan(
        state=TaskState.QUEUED,
        ticket=ticket,
        preprocessed=preprocessed,
        reason="planned",
        repos=target_repos,
        understanding=understanding,
        selection=selection,
        system_sections=sections,
        brief=brief,
        transitions=tuple(machine.history),
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
#: Sent on a retry. Short on purpose: the check output is already in the log as a
#: `task/check` event the model can see, so repeating it here would duplicate
#: context and imply the two could disagree.
FIX_PROMPT = (
    "The project's checks failed. Read the output above, fix the cause, and stop "
    "once you believe the checks will pass."
)


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """What running the plan produced."""

    state: TaskState
    reason: str = ""
    attempts: int = 0
    turns: int = 0
    checks: tuple[Any, ...] = ()
    diff: str = ""
    changed_paths: tuple[str, ...] = ()
    #: Test paths held at the base revision while the checks ran. The honest
    #: answer to "was the oracle pinned", which a reviewer needs in order to read
    #: a pass correctly.
    pinned_tests: tuple[str, ...] = ()
    branch: str = ""
    pull_request: Any = None
    #: The drafted pull request when none was opened. The deliverable in that case:
    #: built from the real checks and the real diff, so a human can act on it.
    draft: Any = None
    transitions: tuple[tuple[TaskState, str], ...] = field(default_factory=tuple)

    @property
    def delivered(self) -> bool:
        return self.state is TaskState.DELIVERED


async def run_task(
    *,
    plan: TaskPlan,
    project: ProjectConfig,
    session: Any,
    llm: Any,
    tools: Any,
    vcs: Any,
    workspace: Any,
    sandbox: Any,
    constraints: ExecutionConstraints,
    pipeline: Any = None,
    tracker: Any = None,
    model: str = "",
    author: Any = None,
    #: The decision authority, the answerer, and who is calling — needed for the
    #: one part of a task that is not a tool call. See `runtime/authority.py`.
    policy: Any = None,
    approval: Any = None,
    principal: Any = None,
    #: Optional compaction. `None` keeps the loud overflow failure as the only
    #: thing that happens at the window, which is what task mode did before.
    compactor: Any = None,
    #: The step ceiling for **each attempt's turn**. `run_task` drives `run_turn`
    #: directly rather than through an agent, so it never saw the configured
    #: `max_steps` and every task ran on the default — a profile setting that only
    #: chat honoured. `attempts × max_steps` is the real bound on a task.
    max_steps: int = DEFAULT_MAX_STEPS,
) -> TaskOutcome:
    """Execute a plan: branch, work, check, repair, deliver.

    Delivery is **harness-driven, not model-driven**. The model never gets an
    `open_pr` tool, because opening a pull request is a privileged, outward-facing
    act and the fixed workflow already decided when it should happen — once the
    checks pass. Giving the model the tool would add an authority it has no need
    for, and every capability that exists is one that can be induced.
    """
    from forgepilot.runtime.authority import authorise
    from forgepilot.runtime.loop.turn import run_turn
    from forgepilot.runtime.pinning import pinned_to_base
    from forgepilot.runtime.repair import combine_checks, run_checks
    from forgepilot.seams.agent_loop import UserInput
    from forgepilot.seams.security import Operation
    from forgepilot.seams.session import EventKind, SurfaceOp
    from forgepilot.seams.vcs import PullRequestDraft
    from forgepilot.seams.workspace import NothingToCommit

    machine = TaskStateMachine()

    if not plan.ready:
        return TaskOutcome(state=plan.state, reason=plan.reason)

    assert plan.selection is not None  # noqa: S101 - invariant of `ready`

    machine.to(TaskState.RUNNING, "executing plan")
    await session.append(
        EventKind.TASK_STATE,
        {"state": str(TaskState.RUNNING), "ticket": str(plan.ticket.id), "toolSet": plan.selection.tool_set},
    )

    repo = plan.repos[0]
    session_id = session.header.session_id

    # **The mirror comes first, and it is called unconditionally.** A worktree
    # cannot be cut from a repository that is not on this machine yet, and `vcs`
    # is the only thing that knows how to reach a repo host.
    #
    # Not gated on a capability. `ensure_mirror` is a *mandatory* Protocol method,
    # not an optional extra — the shipped providers implement it as a no-op
    # because their mirror is the local checkout, which is also why nobody
    # noticed it had no caller at all until a provider existed whose mirror has
    # to be fetched.
    await vcs.ensure_mirror(repo, constraints)

    # `prepare` already made a branch: a worktree is checked out *onto* one. The
    # old `vcs.create_branch(repo, handle.branch, …)` was creating a second name
    # for a branch the workspace had already made, which is the sort of thing that
    # only looks reasonable while nothing is real.
    handle = await workspace.prepare(repo, project.default_branch, session_id)
    branch = handle.branch

    # Enforce the routing selection at the point of execution. Restricting the
    # schemas the model is *shown* is not enough: a model can name a tool it was
    # never offered, and the registry would run it.
    if pipeline is not None:
        pipeline.allowed_tools = plan.selection.tools

    checks: list[Any] = []
    turns = 0
    passed = False
    attempts = 0
    pinned: tuple[str, ...] = ()

    for attempt in range(project.retry.max_attempts + 1):
        attempts = attempt + 1
        await run_turn(
            session=session,
            user_input=UserInput(text=plan.brief if attempt == 0 else FIX_PROMPT),
            llm=llm,
            tools=tools,
            constraints=constraints,
            model=model,
            system_sections=plan.system_texts,
            workspace_root=handle.root,
            max_steps=max_steps,
            tool_schemas=plan.tool_schemas(tools),
            tracker=tracker,
            compactor=compactor,
        )
        turns += 1

        # **The oracle is held fixed.** Whatever the agent changed under the
        # project's test paths is set aside for the duration of the check, so a
        # weakened assertion cannot be what makes this pass. This is per attempt
        # rather than once, because each turn may edit the tests again. See
        # `runtime/pinning.py` for why this is a mechanism rather than a warning.
        pins = project.tests_in(tuple(await workspace.changed_paths(handle)))
        if pins:
            # **Two trees, both must pass.** Pinned alone cannot see a feature —
            # restoring the tests to base deletes the specification for something
            # that does not exist yet. Agent alone cannot see a weakened
            # assertion. See `repair.combine_checks`.
            async with pinned_to_base(workspace, handle, pins) as applied:
                # The union across attempts, not the last one: the report is "what
                # was kept out of the deciding run", and a path pinned on the first
                # attempt and reverted by the agent on the second was still kept out
                # of a run whose verdict is in this outcome.
                pinned = tuple(dict.fromkeys((*pinned, *applied)))
                strict = await run_checks(
                    commands=project.test,
                    sandbox=sandbox,
                    session=session,
                    constraints=constraints,
                    workspace_root=handle.root,
                    session_id=session_id,
                    pinned=applied,
                )
            extended = await run_checks(
                commands=project.test,
                sandbox=sandbox,
                session=session,
                constraints=constraints,
                workspace_root=handle.root,
                session_id=session_id,
            )
            check = combine_checks((("pinned", strict), ("agent", extended)))
            # The extension is already in the log from its own run, so recording
            # the conjunction would double every check event. Re-record the one
            # that decides, so a reader sees the verdict the task acted on.
            await session.append(
                EventKind.TASK_CHECK,
                {
                    "passed": check.passed,
                    "command": check.command,
                    "commands": list(check.commands),
                    "exitCode": check.exit_code,
                    "output": check.output,
                    "couldNotRun": check.could_not_run,
                    "reason": check.reason,
                    "meta": dict(check.meta),
                },
                surface=SurfaceOp.APPEND,
            )
        else:
            check = await run_checks(
                commands=project.test,
                sandbox=sandbox,
                session=session,
                constraints=constraints,
                workspace_root=handle.root,
                session_id=session_id,
            )
        checks.append(check)

        if check.passed:
            passed = True
            break
        if check.could_not_run:
            # Retrying something that cannot run just burns budget. Say why and
            # stop rather than looping against a wall.
            break

    diff = await workspace.diff(handle)
    changed = tuple(await workspace.changed_paths(handle))

    if not passed:
        reason = checks[-1].reason if checks and checks[-1].could_not_run else "checks failed"
        machine.to(TaskState.FAILED, reason)
        await session.append(
            EventKind.TASK_STATE, {"state": str(TaskState.FAILED), "reason": reason}
        )
        # A finished task's log must be durable. Without this the closing events
        # sit in the buffer, and a reader — or a crash — sees a task that never
        # ended.
        await session.flush()
        return TaskOutcome(
            state=TaskState.FAILED,
            reason=reason,
            attempts=attempts,
            turns=turns,
            checks=tuple(checks),
            diff=diff,
            changed_paths=changed,
            pinned_tests=pinned,
            branch=branch,
            transitions=tuple(machine.history),
        )

    from forgepilot.seams.refs import CommitAuthor

    # The commit belongs to the working tree, so the workspace makes it — the
    # tree is what is being committed, and `vcs` names a repository without ever
    # having been told which checkout it meant.
    try:
        await workspace.commit(
            handle,
            f"fix: {plan.ticket.title}",
            author or CommitAuthor(name="ForgePilot", email="forgepilot@localhost"),
            constraints,
        )
    except NothingToCommit as exc:
        # The checks passed and the tree never changed. Reachable whenever the
        # ticket describes something already fixed, or a write was refused — and
        # a crash with git's own wording at the last step of a run that appeared
        # to succeed is the wrong way to say "no change was produced".
        reason = f"the checks passed but nothing was changed, so there is nothing to deliver: {exc}"
        machine.to(TaskState.FAILED, reason)
        await session.append(
            EventKind.TASK_STATE, {"state": str(TaskState.FAILED), "reason": reason}
        )
        await session.flush()
        return TaskOutcome(
            state=TaskState.FAILED,
            reason=reason,
            attempts=attempts,
            turns=turns,
            checks=tuple(checks),
            diff=diff,
            changed_paths=tuple(changed),
            pinned_tests=pinned,
            branch=branch,
            transitions=tuple(machine.history),
        )
    draft = PullRequestDraft(
        repo=repo,
        source_branch=branch,
        target_branch=project.default_branch,
        title=f"fix: {plan.ticket.title}",
        body_md=_pull_request_body(plan, checks, changed, diff, pinned),
        # Traceable back to the work item, not only to a branch name.
        ticket_id=str(plan.ticket.id),
        draft=True,
    )

    # Guarded like the pull request below it, and for the same reason: a push
    # needs a remote. A provider that says it cannot push is telling the truth
    # about this deployment, and calling it anyway would turn a configuration
    # fact into a crash at the last step of a task that otherwise succeeded.
    published = "push" in vcs.capabilities()

    # --- the harness's own outward-facing act -----------------------------
    #
    # **This is the part no pre-execute listener can ever see.** Pushing a branch
    # and opening a pull request are not tool calls: there is no `tool/call`, no
    # armed capability, no `describe_targets`. So `ask_operations: [open_pr]`
    # named precisely the operation that never reached the mechanism it
    # configures, and the honest reading of that config file was wrong.
    #
    # Both acts are authorised together, on `OPEN_PR`, because they are one
    # publication step: nothing is pushed unless a pull request is about to be
    # opened. There is no `PUSH` operation, and inventing one here would be this
    # function deciding what the coarse operation set means.
    if published:
        authorisation = await authorise(
            policy=policy,
            approval=approval,
            principal=principal,
            operation=Operation.OPEN_PR,
            session=session,
            summary=(
                f"publish {len(changed)} changed file(s) on {repo.slug} as branch "
                f"{branch!r} and open a draft pull request against "
                f"{project.default_branch!r}. Checks: "
                + "; ".join(
                    f"{c.command} -> {'passed' if c.passed else 'FAILED'}" for c in checks
                )
            ),
        )
        if not authorisation.allowed:
            # `READY`, not `FAILED`: the work is done, committed and verified,
            # and the only thing missing is permission to publish it. That is
            # exactly what `READY` means — "done, not published" — and the draft
            # goes back with the outcome so a person can raise it by hand.
            machine.to(TaskState.READY, authorisation.reason)
            await session.append(
                EventKind.TASK_STATE,
                {
                    "state": str(TaskState.READY),
                    "branch": branch,
                    "pullRequest": None,
                    "reason": authorisation.reason,
                    "asked": authorisation.asked,
                },
            )
            await session.flush()
            return TaskOutcome(
                state=TaskState.READY,
                reason=authorisation.reason,
                attempts=attempts,
                turns=turns,
                checks=tuple(checks),
                diff=diff,
                changed_paths=tuple(changed),
                pinned_tests=pinned,
                branch=branch,
                draft=draft,
                transitions=tuple(machine.history),
            )

        await vcs.push(repo, branch, constraints)

    # Publishing needs a **repo host** — the service that owns pull requests.
    # Without one there is nothing to open against: the seam hands us a path, a
    # branch, a commit, a diff and a body, but a pull request is an object the
    # host owns, with a number it assigned. Inventing a reference would be the
    # mock-that-lies failure this project keeps running into.
    #
    # So the capability decides, and `capabilities()` — the seam's own mechanism
    # for saying what a provider can do — is consulted for the first time here.
    if not published or "pull_requests" not in vcs.capabilities():
        machine.to(
            TaskState.READY,
            "checks passed and the draft is written; no repo host is configured, so "
            "nothing was published",
        )
        await session.append(
            EventKind.TASK_STATE,
            {
                "state": str(TaskState.READY),
                "branch": branch,
                # Explicitly absent, so a reader does not have to infer it.
                "pullRequest": None,
                "reason": "no repo host configured",
            },
        )
        await session.flush()
        return TaskOutcome(
            state=TaskState.READY,
            reason=(
                f"the change is committed on the local branch {branch!r} and the checks "
                f"passed, but this deployment has no repo host configured, so nothing was "
                f"pushed and no pull request was opened. The draft is ready to be raised "
                f"by hand, or by a deployment that has a host provider."
            ),
            attempts=attempts,
            turns=turns,
            checks=tuple(checks),
            diff=diff,
            changed_paths=tuple(changed),
            pinned_tests=pinned,
            branch=branch,
            draft=draft,
            transitions=tuple(machine.history),
        )

    pull_request = await vcs.open_pull_request(draft, constraints)

    machine.to(TaskState.DELIVERED, "checks passed and a pull request was opened")
    await session.append(
        EventKind.TASK_STATE,
        {"state": str(TaskState.DELIVERED), "pullRequest": pull_request.url},
    )
    await session.flush()

    return TaskOutcome(
        state=TaskState.DELIVERED,
        reason="delivered",
        attempts=attempts,
        turns=turns,
        checks=tuple(checks),
        diff=diff,
        changed_paths=changed,
        pinned_tests=pinned,
        branch=branch,
        pull_request=pull_request,
        transitions=tuple(machine.history),
    )


def _pull_request_body(
    plan: TaskPlan,
    checks: Sequence[Any],
    changed: Sequence[str],
    diff: str,
    pinned: Sequence[str] = (),
) -> str:
    """Assemble the PR description from what actually happened.

    Built from the log rather than asked of the model: the evidence is already
    recorded, and a model-authored summary of its own work is the least reliable
    available source for it. A summarisation turn could add narrative later; the
    facts should not depend on one.
    """
    lines = [
        f"Fixes {plan.ticket.id}.",
        "",
        "## Checks",
    ]
    for check in checks:
        status = "passed" if check.passed else "FAILED"
        lines.append(f"- `{check.command}` — {status}")
    if pinned:
        # A reviewer reading "checks passed" has to know what they were run
        # against, or the sentence means nothing in the one case it matters.
        lines.append("")
        lines.append("## ⚠ Tests were modified, and were not what the checks ran against")
        lines.append(
            "The paths below were restored to this task's base revision for the check, "
            "so a change to them could not be what made it pass. Their changes are in "
            "the diff and still need reviewing."
        )
        lines.extend(f"- `{path}`" for path in pinned)
    if changed:
        lines.append("")
        lines.append("## Changed")
        lines.extend(f"- `{path}`" for path in changed)
    if plan.understanding is not None and plan.understanding.symptom:
        lines.append("")
        lines.append("## Reported symptom")
        lines.append(plan.understanding.symptom)
    if diff:
        lines.append("")
        lines.append("<details><summary>Diff</summary>")
        lines.append("")
        lines.append("```diff")
        lines.append(diff.rstrip())
        lines.append("```")
        lines.append("</details>")
    return "\n".join(lines)
