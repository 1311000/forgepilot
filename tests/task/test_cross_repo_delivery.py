"""M13: one ticket, several repositories — and each one delivered on its own.

The unit of delivery is a `(session, repository)` pair: one working tree, one
commit, one pull request. A terminal state and a pass rate cannot show the defect
this file exists to rule out — a run that fixed one repository, never looked at the
other, and reported success — so the assertions below are about *per-repository*
facts: which tree each change landed in, which deliveries were committed, and what
the report says about the repositories the run never reached.

The end-to-end tests use the `cross_repo_task_rig` fixture, which stands up three
real git repositories (`tests/conftest.py`, `CROSS_PROJECT`).
"""

from __future__ import annotations

import json
from pathlib import Path

from forgepilot.orchestration import ClassifierGateway
from forgepilot.orchestration.context_assembly import compose_delivery_brief
from forgepilot.orchestration.modes.task import (
    _NOT_ATTEMPTED,
    RepoDelivery,
    TaskPlan,
    _pull_request_body,
    _split_section,
    _task_verdict,
    plan_task,
    run_task,
)
from forgepilot.orchestration.preprocess import PreprocessResult
from forgepilot.orchestration.task_state import TaskState
from forgepilot.providers.llm.replay import text_turn, tool_call_turn
from forgepilot.providers.sandbox.scripted import ScriptedResult
from forgepilot.seams.classifier import TicketUnderstanding
from forgepilot.seams.refs import RepoRef, TicketId
from forgepilot.seams.session import EventKind
from forgepilot.seams.tickets import Ticket, TicketPriority
from forgepilot.seams.vcs import PullRequestRef

#: These three slugs are the ones `tests/conftest.py`'s `CROSS_PROJECT` declares and
#: the fixture builds checkouts for. Spelled out here rather than imported, because
#: pytest does not put the `tests` package on `sys.path`.
REPO_A = RepoRef(host="git.corp", namespace="team", name="svc-a")
REPO_B = RepoRef(host="git.corp", namespace="team", name="svc-b")
REPO_C = RepoRef(host="git.corp", namespace="team", name="svc-c")

FIX_A = (
    "MAX_ATTEMPTS = 3\n\n\ndef retry_login(client, credentials):\n"
    "    for _ in range(MAX_ATTEMPTS):\n        try:\n"
    "            return client.login(credentials)\n        except AuthError:\n"
    "            continue\n    raise RuntimeError('login failed after retries')\n"
)
FIX_B = "def summarise(rows):\n    total = 0\n    for row in rows:\n        total += row\n    return total\n"


# ---------------------------------------------------------------------------
# Building a run
# ---------------------------------------------------------------------------
class FixedClassifier:
    """Always the same confident reading, and it never declines."""

    def __init__(self, understanding: TicketUnderstanding) -> None:
        self.understanding = understanding

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    async def classify(self, request, *, session=None):  # noqa: ANN001
        return self.understanding


def a_ticket_naming(*repos: RepoRef) -> Ticket:
    """A complete ticket whose *text* names the repositories it spans.

    That is where the planner reads them from, and where a person would put them:
    M13 makes plurality come from the ticket, never from the project's own list.
    """
    named = "、".join(repo.slug for repo in repos)
    return Ticket(
        id=TicketId("T-7"),
        title="报表合计偏小",
        description_md=(
            "复现步骤：\n1. 打开报表页\n\n预期结果：合计与明细一致\n实际结果：合计偏小\n\n"
            f"改动涉及 {named}。"
        ),
        priority=TicketPriority.HIGH,
    )


def a_plan(*repos: RepoRef, brief: str = "do the work") -> TaskPlan:
    """A ready plan over `repos`, assembled without running the planner."""
    ticket = a_ticket_naming(*repos)
    return TaskPlan(
        state=TaskState.QUEUED,
        ticket=ticket,
        preprocessed=PreprocessResult(ticket=ticket, repos=repos),
        repos=repos,
        brief=brief,
    )


async def plan_for(rig, *repos: RepoRef):
    return await plan_task(
        ticket=a_ticket_naming(*repos),
        project=rig.project,
        constraints=rig.constraints,
        registry=rig.registry,
        classifier=ClassifierGateway(
            FixedClassifier(
                TicketUnderstanding(category="bug", symptom="合计偏小", confidence=0.95)
            )
        ),
    )


async def run(rig, plan, **kwargs):
    return await run_task(
        plan=plan,
        project=rig.project,
        session=rig.session,
        llm=rig.llm,
        tools=rig.registry,
        vcs=rig.vcs,
        workspace=rig.workspace,
        sandbox=rig.sandbox,
        constraints=rig.constraints,
        pipeline=rig.pipeline,
        tracker=rig.tracker,
        model="replay-1",
        policy=rig.policy,
        approval=rig.approval,
        principal=rig.principal,
        **kwargs,
    )


def write_file(path: str, content: str):
    return tool_call_turn(
        "write_file", json.dumps({"path": path, "content": content}), call_id=f"c-{path}"
    )


def state_events(session) -> list[dict]:
    return [
        dict(event.data)
        for event in session.committed_events
        if event.kind is EventKind.TASK_STATE
    ]


# ---------------------------------------------------------------------------
# The brief: which tree this turn is standing in
# ---------------------------------------------------------------------------
def test_a_single_repository_brief_names_the_tree_and_says_nothing_else() -> None:
    brief = compose_delivery_brief(
        repo=REPO_A, branch="forgepilot/s/svc-a", root=Path("/work/svc-a")
    )

    assert f"Repository: {REPO_A.slug}" in brief
    assert f"Working tree: {Path('/work/svc-a')}" in brief
    assert "Branch: forgepilot/s/svc-a" in brief
    assert "not** in this tree" not in brief, (
        "a ticket for one repository has no siblings to warn about"
    )


def test_a_plural_brief_says_the_siblings_files_are_not_in_this_tree() -> None:
    brief = compose_delivery_brief(
        repo=REPO_B,
        branch="forgepilot/s/svc-b",
        root=Path("/work/svc-b"),
        siblings=(REPO_A, REPO_C),
    )

    assert f"Repository: {REPO_B.slug}" in brief
    assert REPO_A.slug in brief and REPO_C.slug in brief
    # The sentence that stops a model reporting success for a change it could not
    # have made: the other repositories' files are simply not here.
    assert "their files are **not** in this tree" in brief
    assert "This ticket covers 3 repositories" in brief


# ---------------------------------------------------------------------------
# The pull request body: what happened to the rest of the ticket
# ---------------------------------------------------------------------------
def test_a_single_repository_body_is_byte_for_byte_what_it_always_was() -> None:
    plan = a_plan(REPO_A)

    unchanged = _pull_request_body(plan, (), ("src/login.py",), "diff")
    with_repo = _pull_request_body(plan, (), ("src/login.py",), "diff", repo=REPO_A)

    assert with_repo == unchanged, (
        "passing the delivery's repository must add nothing for a one-repository "
        "ticket; every body this system has opened reads as it did"
    )
    assert "other repositories" not in with_repo


def test_a_plural_body_says_where_the_other_repositories_stand() -> None:
    plan = a_plan(REPO_A, REPO_B, REPO_C)
    opened = [
        RepoDelivery(repo=REPO_A, state=TaskState.DELIVERED),
        RepoDelivery(
            repo=REPO_B,
            state=TaskState.READY,
            reason="no repo host configured",
            branch="forgepilot/s/svc-b",
            draft=object(),
        ),
    ]

    lines = _split_section(plan, opened, repo=REPO_A)

    assert lines[1] == "## This ticket's other repositories"
    assert any("**this pull request**" in line and REPO_A.slug in line for line in lines)
    assert any("not published" in line and REPO_B.slug in line for line in lines)
    assert any(line.startswith(f"- `{REPO_C.slug}`") and "not attempted" in line for line in lines), (
        "a repository the run never reached is described as exactly that, not "
        "omitted: the reader has to know the ticket was bigger than this pull request"
    )


def test_a_sibling_already_opened_is_named_by_its_number() -> None:
    plan = a_plan(REPO_A, REPO_B)
    opened = [
        RepoDelivery(
            repo=REPO_B,
            state=TaskState.DELIVERED,
            pull_request=PullRequestRef(repo=REPO_B, number=12, url="https://host/pr/12"),
        )
    ]

    lines = _split_section(plan, opened, repo=REPO_A)

    assert any(f"- `{REPO_B.slug}` — #12" == line for line in lines)


def test_a_broken_sibling_is_reported_as_failed_with_its_reason() -> None:
    plan = a_plan(REPO_A, REPO_B)
    opened = [RepoDelivery(repo=REPO_B, state=TaskState.FAILED, reason="checks failed")]

    lines = _split_section(plan, opened, repo=REPO_A)

    assert any("**failed**: checks failed" in line for line in lines)


# ---------------------------------------------------------------------------
# The task's verdict, derived from its deliveries
# ---------------------------------------------------------------------------
def delivery(repo: RepoRef, state: TaskState, **kwargs) -> RepoDelivery:
    return RepoDelivery(repo=repo, state=state, **kwargs)


def test_one_delivery_is_reported_verbatim_however_it_ended() -> None:
    only = delivery(REPO_A, TaskState.READY, reason="no repo host configured", branch="b")

    assert _task_verdict([only]) == (TaskState.READY, "no repo host configured")


def test_no_delivery_is_a_failure_that_admits_there_was_nothing_to_do() -> None:
    state, reason = _task_verdict([])

    assert state is TaskState.FAILED
    assert "nothing to deliver" in reason


def test_every_repository_published_is_delivered() -> None:
    state, reason = _task_verdict(
        [delivery(REPO_A, TaskState.DELIVERED), delivery(REPO_B, TaskState.DELIVERED)]
    )

    assert state is TaskState.DELIVERED
    assert "delivered to every repository" in reason


def test_done_but_not_published_is_ready_not_delivered() -> None:
    state, _ = _task_verdict(
        [
            delivery(REPO_A, TaskState.DELIVERED),
            delivery(REPO_B, TaskState.READY, reason="draft only"),
        ]
    )

    assert state is TaskState.READY


def test_one_failure_makes_the_task_fail_and_the_summary_names_every_repository() -> None:
    state, reason = _task_verdict(
        [
            delivery(REPO_A, TaskState.DELIVERED),
            delivery(REPO_B, TaskState.FAILED, reason="checks failed"),
            delivery(REPO_C, TaskState.QUEUED, reason=_NOT_ATTEMPTED),
        ]
    )

    assert state is TaskState.FAILED
    assert reason.startswith("failed: ")
    assert f"{REPO_A.slug} (delivered)" in reason
    assert f"{REPO_B.slug} (failed)" in reason, "the summary has to say which one failed"
    assert f"{REPO_C.slug} (not attempted)" in reason, (
        "a repository the run stopped before is a fact in the report, not an absence"
    )
    assert "checks failed" in reason


# ---------------------------------------------------------------------------
# End to end: two repositories, two of everything
# ---------------------------------------------------------------------------
async def test_two_repositories_get_two_trees_two_commits_and_two_pull_requests(
    cross_repo_task_rig,
) -> None:
    rig = cross_repo_task_rig
    plan = await plan_for(rig, REPO_A, REPO_B)
    assert plan.ready, plan.describe()
    assert [repo.slug for repo in plan.repos] == [REPO_A.slug, REPO_B.slug]

    rig.llm.script(
        write_file("src/login.py", FIX_A),
        text_turn("bounded the retry loop"),
        write_file("src/report.py", FIX_B),
        text_turn("summarise now totals the rows"),
    )
    rig.sandbox.script(
        ScriptedResult(exit_code=0, stdout="1 passed"),
        ScriptedResult(exit_code=0, stdout="1 passed"),
    )

    outcome = await run(rig, plan)

    assert outcome.state is TaskState.DELIVERED, outcome.reason
    assert [d.state for d in outcome.deliveries] == [
        TaskState.DELIVERED,
        TaskState.DELIVERED,
    ]
    first, second = outcome.deliveries

    # Each delivery changed **its own** repository's file. If the second one lists
    # `src/login.py`, the loop handed both repositories the same tree — which is
    # the Phase A defect, one level up.
    assert first.changed_paths == ("src/login.py",)
    assert second.changed_paths == ("src/report.py",)

    # Two trees, two branches, two commits — and each name carries its repository,
    # so a log or a `git branch` listing says which work is which. The slug is
    # sanitised into the ref (`.` and `/` cannot appear in a branch name), so the
    # assertion is on the repository's own name rather than on the whole slug.
    assert first.branch != second.branch
    assert "svc-a" in first.branch and "svc-b" in second.branch
    assert first.commit is not None and second.commit is not None
    assert first.commit.sha != second.commit.sha

    assert len(rig.vcs.pull_requests) == 2
    numbers = sorted(draft.repo.slug for draft in rig.vcs.pull_requests)
    assert numbers == sorted([REPO_A.slug, REPO_B.slug])

    bodies = {draft.repo.slug: draft.body_md for draft in rig.vcs.pull_requests}
    assert REPO_B.slug in bodies[REPO_A.slug], (
        "A's body has to say what happened to B, or a reviewer reads a "
        "cross-repository change as a complete one"
    )
    assert "#1" in bodies[REPO_B.slug], "B can name A's pull request: it is already open"


# ---------------------------------------------------------------------------
# End to end: a failure stops the run and the rest are reported
# ---------------------------------------------------------------------------
async def test_a_failed_delivery_stops_the_run_and_the_rest_are_not_attempted(
    cross_repo_task_rig,
) -> None:
    rig = cross_repo_task_rig
    plan = await plan_for(rig, REPO_A, REPO_B, REPO_C)
    assert plan.ready, plan.describe()

    rig.llm.script(
        write_file("src/login.py", FIX_A),
        text_turn("bounded the retry loop"),
        # The second repository: the model answers with words and changes nothing,
        # so its check passes over an unchanged tree and the *commit* is what fails.
        # A short road to a failed delivery, and a real one.
        text_turn("the reporting code looks correct to me"),
    )
    rig.sandbox.script(
        ScriptedResult(exit_code=0, stdout="1 passed"),
        ScriptedResult(exit_code=0, stdout="1 passed"),
    )

    outcome = await run(rig, plan)

    assert outcome.state is TaskState.FAILED
    assert [d.state for d in outcome.deliveries] == [
        TaskState.DELIVERED,
        TaskState.FAILED,
        TaskState.QUEUED,
    ]
    delivered, failed, never = outcome.deliveries

    assert "nothing was changed" in failed.reason
    assert failed.commit is None and failed.pull_request is None
    assert failed.branch, "the failed delivery still stood in a tree of its own"

    assert never.reason == _NOT_ATTEMPTED
    assert never.attempted is False
    assert never.branch == "" and never.commit is None and never.changed_paths == (), (
        "a repository the run never reached has no tree, no branch and no change"
    )

    # One pull request, not three: the first repository was published before the
    # second was attempted, and the third was never started.
    assert len(rig.vcs.pull_requests) == 1
    assert rig.vcs.pull_requests[0].repo.slug == REPO_A.slug

    assert REPO_B.slug in outcome.reason and REPO_C.slug in outcome.reason
    assert outcome.attempts == 2, "one attempt per delivery that started"
    assert outcome.turns == 2, "one model turn per attempt; the third repository's turn never happened"


async def test_the_log_records_a_state_per_delivery_and_never_a_second_verdict(
    cross_repo_task_rig,
) -> None:
    """The task's own verdict is on the outcome, not a duplicate of the deliveries'.

    One `running` at the top, then exactly one state event per repository that was
    attempted. A single-repository run therefore still reads `running`, `delivered`
    — which is what every log written before M13 reads.
    """
    rig = cross_repo_task_rig
    plan = await plan_for(rig, REPO_A, REPO_B)

    rig.llm.script(
        write_file("src/login.py", FIX_A),
        text_turn("bounded the retry loop"),
        write_file("src/report.py", FIX_B),
        text_turn("summarise now totals the rows"),
    )
    rig.sandbox.script(
        ScriptedResult(exit_code=0, stdout="1 passed"),
        ScriptedResult(exit_code=0, stdout="1 passed"),
    )

    await run(rig, plan)

    events = state_events(rig.session)
    assert [event["state"] for event in events] == ["running", "delivered", "delivered"]
    assert [event.get("repo") for event in events] == [None, REPO_A.slug, REPO_B.slug], (
        "with several deliveries, `the task delivered` does not say which work did"
    )
    assert events[0]["repositories"] == [REPO_A.slug, REPO_B.slug]

    checks = [
        dict(event.data)
        for event in rig.session.committed_events
        if event.kind is EventKind.TASK_CHECK
    ]
    assert [check.get("repo") for check in checks] == [REPO_A.slug, REPO_B.slug], (
        "each repository's check ran in its own tree, and the log says which"
    )
