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

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgepilot.config.project import ProjectConfig
from forgepilot.orchestration.classifier import ClassifierGateway
from forgepilot.orchestration.context_assembly import (
    PromptSection,
    assemble_system_sections,
    compose_delivery_brief,
    compose_task_brief,
)
from forgepilot.orchestration.preprocess import PreprocessResult, preprocess
from forgepilot.orchestration.router import AdapterSelection, select_adapters
from forgepilot.orchestration.task_state import TaskState, TaskStateMachine
from forgepilot.runtime.loop.turn import DEFAULT_MAX_STEPS
from forgepilot.seams.classifier import ClassificationRequest, TicketUnderstanding
from forgepilot.seams.refs import RepoRef
from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.tickets import Ticket
from forgepilot.seams.vcs import Vcs

__all__ = ["TaskPlan", "plan_task"]


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """What this task would run with, or why it cannot yet run at all."""

    state: TaskState
    ticket: Ticket
    preprocessed: PreprocessResult
    reason: str = ""
    #: Which repositories the work targets — **all of them**, and each one is a
    #: delivery of its own (M13: one worktree, one commit, one pull request). Might
    #: have been `Any` until then; a tuple of `RepoRef` is what it always held.
    repos: tuple[RepoRef, ...] = ()
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
        # The repositories come first because since M13 they are what a reader has
        # to check: a plan for two of them is a plan for two pull requests.
        repositories = ", ".join(r.slug for r in self.repos)
        noun = "repository" if len(self.repos) == 1 else "repositories"
        line = (
            f"ready: {noun} {repositories}, tool set "
            f"{self.selection.tool_set!r}, engine {self.selection.engine!r}, "
            f"{len(self.selection.tools)} tool(s)"
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

    # Which repositories? Answered by rules, and answered *before* the classifier
    # for the same reason as above. A ticket naming a repository nobody knows
    # cannot be scoped, and asking a model to guess one would be asking it to
    # choose where to write.
    #
    # ⚠️ **Plurality comes from the ticket, and never from configuration.**
    #
    # `preprocess` returns *every* known repository the ticket names, because that is
    # what the ticket says. M13 made that plurality real: execution delivers to each
    # one — one worktree, one commit and one pull request per repository — so a plan
    # naming two targets is no longer a plan an execution quietly halves. Until M13
    # this was refused here (`AWAITING_INFO`, "this ticket targets 2 repositories …
    # Split the ticket"), and the refusal *was* the honest state while `run_task`
    # took `plan.repos[0]` and discarded the rest without a word.
    #
    # The project's own list is the fallback **only when it is unambiguous — one
    # repository**. With several declared and none named, the run cannot tell which
    # of them the change belongs in, and that guess is not one wrong edit: it is N of
    # them, each ending in its own pull request. So a silent ticket against a
    # multi-repository project is `awaiting_info` naming them — which is also what
    # keeps the split a pull request body explains a split the *ticket* described,
    # rather than one the configuration assumed.
    #
    # There is deliberately **no cap** on the count (M13/D6): the bound is
    # `project.repos` (a deployment's own list) and the per-repository `OPEN_PR`
    # decision, which is where a human is already asked once per repository.
    # Inventing `max_repos: 8` would be a number with no reason behind it.
    target_repos = preprocessed.repos
    if not target_repos and len(project.repos) == 1:
        target_repos = tuple(project.repos)

    if not target_repos:
        if project.repos:
            reason = (
                f"this ticket names no repository, and this project declares "
                f"{len(project.repos)} ({', '.join(r.slug for r in project.repos)}): "
                f"name the repository the change belongs in, or the several it spans"
            )
        else:
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
class RepoDelivery:
    """One repository's delivery — the unit M13 makes plural.

    A task delivers to **every** repository its plan names: one working tree, one
    commit and one pull request each. This is the record of one of them, and it is
    deliberately the only place those facts live. A `TaskOutcome` carrying a
    `branch`, a `diff` and a `pull_request` could not describe a two-repository run
    without either picking one of them and lying or spending a paragraph saying
    which one it meant — and this tree has already paid twice for fields that looked
    enforced and were not (`docs/notes/defect-shape.md`).
    """

    repo: RepoRef
    state: TaskState
    reason: str = ""
    branch: str = ""
    #: The `CommitRef` the workspace made, when it made one.
    commit: Any = None
    diff: str = ""
    changed_paths: tuple[str, ...] = ()
    #: Test paths held at the base revision while the checks ran. The honest answer
    #: to "was the oracle pinned", which a reviewer needs in order to read a pass
    #: correctly.
    pinned_tests: tuple[str, ...] = ()
    checks: tuple[Any, ...] = ()
    attempts: int = 0
    turns: int = 0
    pull_request: Any = None
    #: The drafted pull request when none was opened. The deliverable in that case:
    #: built from the real checks and the real diff, so a human can act on it.
    draft: Any = None
    #: This delivery's own transitions. The task machine records the task's verdict;
    #: one shared machine could not, because `ALLOWED_TRANSITIONS` has no edge from
    #: `delivered` back to `running` for the repository after it.
    transitions: tuple[tuple[TaskState, str], ...] = field(default_factory=tuple)

    @property
    def delivered(self) -> bool:
        return self.state is TaskState.DELIVERED

    @property
    def attempted(self) -> bool:
        """False for a repository the loop never reached.

        Those are recorded as `QUEUED` — nothing ever moved them, which is true —
        and their reason says `not attempted`, so "we stopped before this one" is a
        fact in the report rather than an absence from it.
        """
        return self.state is not TaskState.QUEUED

    def describe(self) -> str:
        """One line, for a human reading the CLI or a report."""
        word = self.state.value if self.attempted else "not attempted"
        parts = [f"{self.repo.slug}: {word}"]
        if self.branch:
            parts.append(f"branch {self.branch!r}")
        if self.changed_paths:
            parts.append(f"{len(self.changed_paths)} changed file(s)")
        if self.pull_request is not None:
            parts.append(f"pull request {self.pull_request.url}")
        elif self.draft is not None:
            parts.append("draft ready")
        if self.reason and self.state is not TaskState.DELIVERED:
            parts.append(self.reason)
        return " — ".join(parts)


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """What running the plan produced: **one record per repository**.

    `state` is the task's own verdict, derived from the deliveries — `DELIVERED`
    when every one of them was published, `READY` when they are done but nothing
    was, `FAILED` when any of them failed. Everything plural lives in
    `deliveries`; there are no single-value echoes of it (see `RepoDelivery`).
    """

    state: TaskState
    reason: str = ""
    #: Sums over the deliveries. `attempts` counts repair rounds and `turns` counts
    #: model turns; both are per repository since M13/D7, which is what
    #: `project.retry.max_attempts` has always meant.
    attempts: int = 0
    turns: int = 0
    deliveries: tuple[RepoDelivery, ...] = ()
    transitions: tuple[tuple[TaskState, str], ...] = field(default_factory=tuple)

    @property
    def delivered(self) -> bool:
        return self.state is TaskState.DELIVERED

    def for_repo(self, slug: str) -> RepoDelivery | None:
        """One repository's delivery, or `None` if the plan never named it."""
        return next((d for d in self.deliveries if d.repo.slug == slug), None)


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
    #: Optional memory store. Unlike the agent loop, task mode **knows** which
    #: repository it is about — the plan names them all — so it does not have to ask
    #: the constraints. `None` means a profile with no `memory` row.
    memory: Any = None,
) -> TaskOutcome:
    """Execute a plan: branch, work, check, repair, deliver — **once per repository**.

    Delivery is **harness-driven, not model-driven**. The model never gets an
    `open_pr` tool, because opening a pull request is a privileged, outward-facing
    act and the fixed workflow already decided when it should happen — once the
    checks pass. Giving the model the tool would add an authority it has no need
    for, and every capability that exists is one that can be induced.

    **One repository at a time, in the plan's order.** A ticket may name several of
    them (M13), and each is delivered on its own: its own working tree, its own
    commit, its own pull request. They run in sequence rather than concurrently
    because there is one session log, one approval path, and `run_turn`'s
    `workspace_root` is a single directory — three things that would each have to be
    made plural before the *deliveries* could be, and a milestone that quietly
    changed all three at once would be impossible to review. The price is stated
    where it is paid: a failure in the second repository does not un-publish the
    first, because **git has no un-push**, and every repository after it is reported
    as not attempted rather than dropped from the report.
    """
    from forgepilot.seams.session import EventKind

    machine = TaskStateMachine()

    if not plan.ready:
        return TaskOutcome(state=plan.state, reason=plan.reason)

    assert plan.selection is not None  # noqa: S101 - invariant of `ready`
    # At least one, not exactly one: M13 made a plan plural. Empty stays impossible
    # because `plan_task` refuses a ticket it cannot place rather than returning an
    # empty plan, and `run_task` should not have to guess what an empty one means.
    assert plan.repos, (  # noqa: S101 - invariant of a ready plan
        "a ready plan must name at least one repository: the planner refuses a "
        "ticket it cannot place, so an empty plan is not a ready one"
    )

    machine.to(TaskState.RUNNING, "executing plan")
    await session.append(
        EventKind.TASK_STATE,
        {
            "state": str(TaskState.RUNNING),
            "ticket": str(plan.ticket.id),
            "toolSet": plan.selection.tool_set,
            # Named rather than counted: the plan is not in the log, so a reader has
            # no other way to learn which repositories this run took on.
            "repositories": [repo.slug for repo in plan.repos],
        },
    )

    # Enforce the routing selection at the point of execution. Restricting the
    # schemas the model is *shown* is not enough: a model can name a tool it was
    # never offered, and the registry would run it. Set once rather than per
    # delivery — it is a fact about the plan, and the plan chose the tool set.
    if pipeline is not None:
        pipeline.allowed_tools = plan.selection.tools

    # Also once: `capabilities()` describes the deployment, not the repository, and
    # asking per delivery would let two deliveries disagree about one provider.
    published = "push" in vcs.capabilities()

    session_id = session.header.session_id
    deliveries: list[RepoDelivery] = []

    for index, repo in enumerate(plan.repos):
        delivery = await _deliver_repository(
            repo=repo,
            # Every *other* repository the ticket names, so the turn can be told
            # whose files are not in its tree. The current one is not in this list.
            siblings=tuple(plan.repos[:index]) + tuple(plan.repos[index + 1 :]),
            # What is already finished, for the pull request body's account of the
            # repositories beside this one.
            opened=tuple(deliveries),
            plan=plan,
            project=project,
            session=session,
            session_id=session_id,
            llm=llm,
            tools=tools,
            vcs=vcs,
            workspace=workspace,
            sandbox=sandbox,
            constraints=constraints,
            tracker=tracker,
            model=model,
            author=author,
            policy=policy,
            approval=approval,
            principal=principal,
            compactor=compactor,
            max_steps=max_steps,
            memory=memory,
            published=published,
        )
        deliveries.append(delivery)
        if delivery.state is TaskState.FAILED:
            break

    # **The repositories the loop never reached still have to appear.** Leaving them
    # out would make a two-repository ticket that delivered one of them
    # indistinguishable from a one-repository ticket, which is the class of silent
    # omission this milestone exists to remove. Their state stays `QUEUED` because
    # nothing ever moved them; their reason says "not attempted".
    for untouched in plan.repos[len(deliveries) :]:
        deliveries.append(
            RepoDelivery(repo=untouched, state=TaskState.QUEUED, reason=_NOT_ATTEMPTED)
        )

    await _rewrite_sibling_sections(
        plan=plan,
        deliveries=deliveries,
        vcs=vcs,
        constraints=constraints,
        session=session,
    )

    state, reason = _task_verdict(deliveries)
    machine.to(state, reason)
    # **The task's own verdict, in the log.** Every `task/state` event so far
    # describes one repository; the verdict the runner returned described all of
    # them and existed only in that return value, so a reader — or a process that
    # restarts and has nothing but the log — could not answer "what happened to
    # this ticket?" without re-deriving the rule. It carries no `repo`, which is
    # how a reader tells the aggregate from a delivery.
    await session.append(
        EventKind.TASK_STATE,
        {
            "state": str(state),
            "reason": reason,
            "repositories": [delivery.repo.slug for delivery in deliveries],
        },
    )
    # A finished task's log must be durable. Without this the closing events sit in
    # the buffer, and a reader — or a crash — sees a task that never ended.
    await session.flush()
    return TaskOutcome(
        state=state,
        reason=reason,
        attempts=sum(delivery.attempts for delivery in deliveries),
        turns=sum(delivery.turns for delivery in deliveries),
        deliveries=tuple(deliveries),
        transitions=tuple(machine.history),
    )

async def _deliver_repository(
    *,
    repo: RepoRef,
    siblings: Sequence[RepoRef],
    opened: Sequence[RepoDelivery],
    plan: TaskPlan,
    project: ProjectConfig,
    session: Any,
    session_id: Any,
    llm: Any,
    tools: Any,
    vcs: Any,
    workspace: Any,
    sandbox: Any,
    constraints: ExecutionConstraints,
    tracker: Any = None,
    model: str = "",
    author: Any = None,
    policy: Any = None,
    approval: Any = None,
    principal: Any = None,
    compactor: Any = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    memory: Any = None,
    published: bool = False,
) -> RepoDelivery:
    """Take **one** repository from a checked-out tree to a finished delivery.

    This is M8's body, moved behind a loop. The work is unchanged; the subject is
    what became plural. It returns a `RepoDelivery` rather than a `TaskOutcome`
    because the task's verdict is a fact about *all* the deliveries, and a function
    holding one repository's variables while deciding it would have to guess.
    """
    from forgepilot.runtime.authority import authorise
    from forgepilot.runtime.loop.turn import run_turn
    from forgepilot.runtime.pinning import pinned_to_base
    from forgepilot.runtime.recall import Recaller
    from forgepilot.runtime.repair import combine_checks, run_checks
    from forgepilot.seams.agent_loop import UserInput
    from forgepilot.seams.refs import CommitAuthor, Mode
    from forgepilot.seams.security import Operation
    from forgepilot.seams.session import EventKind, SurfaceOp
    from forgepilot.seams.vcs import PullRequestDraft
    from forgepilot.seams.workspace import NothingToCommit

    # **A machine per delivery.** The task's own machine records the task's verdict;
    # one machine could not record both, because `ALLOWED_TRANSITIONS` has no edge
    # from `delivered` back to `running`, so the second repository would raise on the
    # way in. These transitions are reported on the delivery, where they happened.
    machine = TaskStateMachine()
    machine.to(TaskState.RUNNING, "executing plan")

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
    #
    # **The handle is keyed on the repository as well as the session** (M13/D2).
    # Before that, a second `prepare` in one session handed back the first
    # repository's tree *and its branch*, and the only symptom was a diff that did
    # not contain the change — a defect with no error message anywhere.
    handle = await workspace.prepare(repo, project.default_branch, session_id)
    branch = handle.branch

    # The ticket text is untrusted and stays in the user turn under its own heading;
    # this block is assembled from the plan and the handle, and it says which tree
    # the turn is standing in — the one fact a plural ticket makes non-obvious.
    brief = (
        compose_delivery_brief(repo=repo, branch=branch, root=handle.root, siblings=siblings)
        + "\n\n"
        + plan.brief
    )

    # Recall is scoped to **this delivery's** repository — which task mode simply
    # *knows*, rather than having to infer it from the caller's authority as the
    # agent loop must — and it runs on the **first attempt only**. A repair turn's
    # input is `FIX_PROMPT` ("the project's checks failed…"), words that describe
    # the situation rather than the work, so matching memories on them would recall
    # almost anything; the block from attempt 0 is still in the conversation anyway.
    recaller = (
        Recaller(memory=memory, scope_ref=repo.slug, mode=Mode.TASK) if memory is not None else None
    )

    checks: list[Any] = []
    turns = 0
    passed = False
    attempts = 0
    pinned: tuple[str, ...] = ()

    for attempt in range(project.retry.max_attempts + 1):
        attempts = attempt + 1
        await run_turn(
            session=session,
            user_input=UserInput(text=brief if attempt == 0 else FIX_PROMPT),
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
            recaller=recaller if attempt == 0 else None,
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
                    repo=repo.slug,
                )
            extended = await run_checks(
                commands=project.test,
                sandbox=sandbox,
                session=session,
                constraints=constraints,
                workspace_root=handle.root,
                session_id=session_id,
                repo=repo.slug,
            )
            check = combine_checks((("pinned", strict), ("agent", extended)))
            # The extension is already in the log from its own run, so recording
            # the conjunction would double every check event. Re-record the one
            # that decides, so a reader sees the verdict the task acted on.
            await session.append(
                EventKind.TASK_CHECK,
                {
                    # Named for the same reason the state events are: with several
                    # deliveries the check events interleave in one log, and a
                    # passing check that does not say which tree it ran in cannot
                    # be told apart from the other repository's.
                    "repo": repo.slug,
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
                repo=repo.slug,
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
            EventKind.TASK_STATE,
            # The repository is on every delivery's state event: with more than one
            # of them, "the task failed" no longer says *which* work failed.
            {"state": str(TaskState.FAILED), "repo": repo.slug, "reason": reason},
        )
        # A finished delivery's log must be durable. Without this the closing events
        # sit in the buffer, and a reader — or a crash — sees work that never ended.
        await session.flush()
        return RepoDelivery(
            repo=repo,
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

    # The commit belongs to the working tree, so the workspace makes it — the
    # tree is what is being committed, and `vcs` names a repository without ever
    # having been told which checkout it meant.
    commit = None
    try:
        commit = await workspace.commit(
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
            EventKind.TASK_STATE,
            {"state": str(TaskState.FAILED), "repo": repo.slug, "reason": reason},
        )
        await session.flush()
        return RepoDelivery(
            repo=repo,
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
        # The body is told what happened to the ticket's *other* repositories, and
        # only the ones already finished: this run publishes as it goes, so a
        # sibling that has not been attempted yet is described as exactly that
        # rather than as a pull request that does not exist.
        body_md=_pull_request_body(plan, checks, changed, diff, pinned, opened=opened, repo=repo),
        # Traceable back to the work item, not only to a branch name.
        ticket_id=str(plan.ticket.id),
        draft=True,
    )

    # `published` is not recomputed here. It is a fact about the deployment, it was
    # asked once in `run_task`, and asking again per repository is how two
    # deliveries would come to disagree about the same provider.

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
                    "repo": repo.slug,
                    "branch": branch,
                    "commit": _commit_sha(commit),
                    # Named rather than counted, so a panel can say *which* files
                    # without a second request to a runner that no longer exists.
                    "changed": list(changed),
                    "pullRequest": None,
                    "reason": authorisation.reason,
                    "asked": authorisation.asked,
                },
            )
            await session.flush()
            return RepoDelivery(
                repo=repo,
                state=TaskState.READY,
                reason=authorisation.reason,
                attempts=attempts,
                turns=turns,
                checks=tuple(checks),
                diff=diff,
                changed_paths=tuple(changed),
                pinned_tests=pinned,
                branch=branch,
                commit=commit,
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
                "repo": repo.slug,
                "branch": branch,
                "commit": _commit_sha(commit),
                "changed": list(changed),
                # Explicitly absent, so a reader does not have to infer it.
                "pullRequest": None,
                "reason": "no repo host configured",
            },
        )
        await session.flush()
        return RepoDelivery(
            repo=repo,
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
            commit=commit,
            draft=draft,
            transitions=tuple(machine.history),
        )

    pull_request = await vcs.open_pull_request(draft, constraints)

    machine.to(TaskState.DELIVERED, "checks passed and a pull request was opened")
    await session.append(
        EventKind.TASK_STATE,
        {
            "state": str(TaskState.DELIVERED),
            "repo": repo.slug,
            "branch": branch,
            "commit": _commit_sha(commit),
            "changed": list(changed),
            "pullRequest": pull_request.url,
        },
    )
    await session.flush()

    return RepoDelivery(
        repo=repo,
        state=TaskState.DELIVERED,
        reason="delivered",
        attempts=attempts,
        turns=turns,
        checks=tuple(checks),
        diff=diff,
        changed_paths=changed,
        pinned_tests=pinned,
        branch=branch,
        commit=commit,
        pull_request=pull_request,
        transitions=tuple(machine.history),
    )


#: The reason a repository the loop never reached carries. Its state stays `QUEUED`
#: because nothing ever moved it — and a run that stops at the first failure has to
#: say so per repository, or the report is silent about the work it did not do.
_NOT_ATTEMPTED = (
    "not attempted: an earlier repository's delivery did not succeed, and the remaining "
    "repositories are not started after a failure"
)


def _commit_sha(commit: Any) -> str | None:
    """The commit as it goes into the log, or `None` when none was made.

    `None` rather than `""`: a commit that does not exist and a commit whose sha
    could not be read are different facts, and an empty string would make the
    second look like the first.
    """
    return None if commit is None else str(commit.sha)



def _task_verdict(deliveries: Sequence[RepoDelivery]) -> tuple[TaskState, str]:
    """The task's own state and reason, derived from its deliveries.

    **One delivery returns its own verdict verbatim.** A single-repository task is
    what every run before M13 was, and its outcome has to read exactly as it did —
    the CLI's exit code, the eval's rows and a human's reading of the log all rest
    on that string. The plural path is entered only when there is something plural
    to say.
    """
    if not deliveries:
        return TaskState.FAILED, "the plan named no repository, so there was nothing to deliver"
    if len(deliveries) == 1:
        return deliveries[0].state, deliveries[0].reason
    failed = [d for d in deliveries if d.state is TaskState.FAILED]
    if failed:
        state, word = TaskState.FAILED, "failed"
    elif all(d.state is TaskState.DELIVERED for d in deliveries):
        state, word = TaskState.DELIVERED, "delivered to every repository"
    else:
        state, word = TaskState.READY, "done, not published"
    spine = ", ".join(
        f"{d.repo.slug} ({d.state.value if d.attempted else 'not attempted'})" for d in deliveries
    )
    detail = "; ".join(
        f"{d.repo.slug}: {d.reason}"
        for d in deliveries
        if d.reason and d.state is not TaskState.DELIVERED
    )
    reason = f"{word}: {spine}"
    if detail:
        reason += f" — {detail}"
    return state, reason


async def _rewrite_sibling_sections(
    *,
    plan: TaskPlan,
    deliveries: Sequence[RepoDelivery],
    vcs: Vcs,
    constraints: ExecutionConstraints,
    session: Any,
) -> None:
    """Give every pull request the sibling account the run can now write.

    Publishing is serial, so the first pull request of a plural ticket is opened
    while the run still knows nothing about the second one — and its body says so:
    "not attempted yet". That sentence was true when it was written and stops being
    the whole truth the moment the next delivery reaches a verdict. A reviewer
    reading pull request #1 should not have to reconstruct the run order to learn
    that #2 exists and passed.

    So the sibling sections are rewritten once, after every delivery has an
    outcome. What is rewritten is exactly the paragraph `_split_section` produces;
    the checks, the diff and the symptom are recomputed from the same recorded
    values, so a rewritten body is the body the run would have written had it known
    the future.

    **A failed rewrite does not fail the task.** The pull request exists and the
    change is in it; the description is stale, which is worth a log event and not
    worth reporting a delivered ticket as failed. The event is where a reader finds
    out that the body they are looking at is the opening one.
    """
    with_pull_request = [d for d in deliveries if d.pull_request is not None]
    if len(plan.repos) < 2 or not with_pull_request:
        return

    from forgepilot.seams.session import EventKind

    finished = tuple(deliveries)
    for delivery in with_pull_request:
        body = _pull_request_body(
            plan,
            delivery.checks,
            delivery.changed_paths,
            delivery.diff or "",
            delivery.pinned_tests,
            opened=finished,
            repo=delivery.repo,
        )
        try:
            await vcs.update_pull_request_body(delivery.pull_request, body, constraints)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            await session.append(
                EventKind.TASK_PUBLISHED,
                {
                    "repo": delivery.repo.slug,
                    "updated": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                },
            )
        else:
            await session.append(
                EventKind.TASK_PUBLISHED,
                {"repo": delivery.repo.slug, "updated": True},
            )


def _split_section(
    plan: TaskPlan,
    opened: Sequence[RepoDelivery],
    *,
    repo: RepoRef,
) -> list[str]:
    """What happened to the ticket's *other* repositories, for a reviewer.

    Empty for a plan that names one repository, which is what keeps every
    single-repository pull request body this system has ever opened
    byte-identical: a paragraph about repositories that were never in the ticket is
    noise, and it would also be a claim about a split that did not happen.

    `opened` is the deliveries this run has finished so far — publishing is
    serial, so a sibling that comes later has not been reached *yet*. The wording
    is time-scoped on purpose: the body is rewritten once every delivery has an
    outcome (`_rewrite_sibling_sections`), and if that rewrite is what fails, the
    sentence it leaves behind still has to be true rather than merely stale.
    """
    if len(plan.repos) < 2:
        return []
    by_slug = {delivery.repo.slug: delivery for delivery in opened}
    lines = [
        "",
        "## This ticket's other repositories",
        "",
        f"This ticket names {len(plan.repos)} repositories, and each is delivered on its "
        "own — one working tree, one commit and one pull request each — because a single "
        "checkout cannot hold two repositories. This pull request carries the change to "
        "one of them:",
        "",
    ]
    for candidate in plan.repos:
        if candidate.slug == repo.slug:
            lines.append(f"- `{candidate.slug}` — **this pull request**")
            continue
        delivery = by_slug.get(candidate.slug)
        if delivery is None:
            lines.append(
                f"- `{candidate.slug}` — not reached yet when this pull request was "
                "opened: the run publishes one repository at a time and stops at the "
                "first failure"
            )
        elif delivery.pull_request is not None:
            lines.append(f"- `{candidate.slug}` — #{delivery.pull_request.number}")
        elif delivery.draft is not None:
            lines.append(
                f"- `{candidate.slug}` — committed on `{delivery.branch}` and verified, but "
                "not published, so it is not in a pull request yet"
            )
        elif delivery.state is TaskState.FAILED:
            lines.append(f"- `{candidate.slug}` — **failed**: {delivery.reason}")
        else:
            lines.append(f"- `{candidate.slug}` — {delivery.describe()}")
    return lines


def _pull_request_body(
    plan: TaskPlan,
    checks: Sequence[Any],
    changed: Sequence[str],
    diff: str,
    pinned: Sequence[str] = (),
    *,
    opened: Sequence[RepoDelivery] = (),
    repo: RepoRef | None = None,
) -> str:
    """Assemble the PR description from what actually happened.

    Built from the log rather than asked of the model: the evidence is already
    recorded, and a model-authored summary of its own work is the least reliable
    available source for it. A summarisation turn could add narrative later; the
    facts should not depend on one.

    `repo` is the delivery this body belongs to. Omitted — as the single-repository
    callers and the tests written before M13 do — the body is exactly what it always
    was.
    """
    lines = [f"Fixes {plan.ticket.id}."]
    if repo is not None:
        lines.extend(_split_section(plan, opened, repo=repo))
    lines.extend(["", "## Checks"])
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
