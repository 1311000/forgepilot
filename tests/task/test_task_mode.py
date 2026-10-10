"""Task mode end to end: a mocked ticket becomes a branch, a diff and a PR draft.

The whole flow runs deterministically against mocks and a **scripted** model, so
it needs no network, no git binary and no test runner. What it does need is a
sandbox that returns output — the recording one refuses every command, and a
repair loop cannot be exercised against a sandbox that never answers.

Three assertions here are the ones worth keeping:

- the diff is **real** — it reflects what the model wrote, not a stub;
- a check that *could not run* is never reported as a pass;
- the harness's check reaches the model as a **user message**, not as a tool
  result the model never asked for (which endpoints reject outright).
"""

from __future__ import annotations

import json

import pytest

from forgepilot.orchestration import ClassifierGateway
from forgepilot.orchestration.modes.task import plan_task, run_task
from forgepilot.providers.llm.replay import text_turn, tool_call_turn
from forgepilot.providers.sandbox.scripted import ScriptedResult
from forgepilot.runtime.projection import derive_messages
from forgepilot.seams.classifier import TicketUnderstanding
from forgepilot.seams.llm import Role
from forgepilot.seams.refs import RepoRef, TicketId
from forgepilot.seams.security import Operation
from forgepilot.seams.session import EventKind
from forgepilot.seams.tickets import Ticket, TicketPriority



# Defined locally rather than imported from conftest: pytest does not put the
# tests package on sys.path, so a relative conftest import fails at collection.
REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")

BUGGY = "MAX_ATTEMPTS = 3\n\n\ndef retry_login(client, credentials):\n    attempts = 0\n    while True:\n        try:\n            return client.login(credentials)\n        except AuthError:\n            attempts += 1\n"

FIXED = "MAX_ATTEMPTS = 3\n\n\ndef retry_login(client, credentials):\n    for _ in range(MAX_ATTEMPTS):\n        try:\n            return client.login(credentials)\n        except AuthError:\n            continue\n    raise RuntimeError('login failed after retries')\n"


class FixedClassifier:
    def __init__(self, understanding: TicketUnderstanding) -> None:
        self.understanding = understanding

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    async def classify(self, request):  # noqa: ANN001
        return self.understanding


def a_ticket() -> Ticket:
    return Ticket(
        id=TicketId("T-1"),
        title="登录无限重试",
        description_md=(
            "复现步骤：\n1. 输错密码\n\n预期结果：提示密码错误\n实际结果：无限重试"
        ),
        priority=TicketPriority.HIGH,
    )


async def make_plan(rig, constraints=None):
    return await plan_task(
        ticket=a_ticket(),
        project=rig.project,
        constraints=constraints or rig.constraints,
        registry=rig.registry,
        classifier=ClassifierGateway(
            FixedClassifier(TicketUnderstanding(category="bug", symptom="无限重试", confidence=0.95))
        ),
    )


async def run(rig, plan, constraints=None, policy=None, principal=None, **kwargs):
    return await run_task(
        plan=plan,
        project=rig.project,
        session=rig.session,
        llm=rig.llm,
        tools=rig.registry,
        vcs=rig.vcs,
        workspace=rig.workspace,
        sandbox=rig.sandbox,
        constraints=constraints or rig.constraints,
        pipeline=rig.pipeline,
        tracker=rig.tracker,
        model="replay-1",
        policy=policy if policy is not None else rig.policy,
        approval=rig.approval,
        principal=principal if principal is not None else rig.principal,
        **kwargs,
    )


def write_fix(content: str):
    return tool_call_turn(
        "write_file", json.dumps({"path": "src/login.py", "content": content}), call_id="c1"
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
async def test_a_ticket_becomes_a_branch_a_diff_and_a_pull_request(task_rig) -> None:
    plan = await make_plan(task_rig)
    assert plan.ready, plan.describe()

    task_rig.llm.script(write_fix(FIXED), text_turn("bounded the retry loop"))
    task_rig.sandbox.script(ScriptedResult(exit_code=0, stdout="1 passed"))

    outcome = await run(task_rig, plan)

    assert outcome.delivered, outcome.reason
    assert outcome.branch
    assert outcome.attempts == 1

    # The diff is real: it reflects what the model wrote, not a placeholder.
    assert "src/login.py" in outcome.diff
    assert "for _ in range(MAX_ATTEMPTS)" in outcome.diff
    assert "while True" in outcome.diff, "the removed line should appear as a removal"
    assert outcome.changed_paths == ("src/login.py",)


async def test_the_pull_request_is_recorded_and_traceable_to_the_ticket(task_rig) -> None:
    plan = await make_plan(task_rig)
    task_rig.llm.script(write_fix(FIXED), text_turn("done"))
    task_rig.sandbox.script(ScriptedResult(exit_code=0, stdout="1 passed"))

    outcome = await run(task_rig, plan)

    assert len(task_rig.vcs.pull_requests) == 1
    draft = task_rig.vcs.pull_requests[0]
    assert draft.ticket_id == "T-1", "a PR must be traceable to its work item"
    assert draft.source_branch == outcome.branch
    assert draft.target_branch == "main"
    assert draft.draft is True
    assert "T-1" in draft.body_md
    assert "pytest -q" in draft.body_md
    assert outcome.pull_request is not None


async def test_the_session_records_the_full_task_trace(task_rig) -> None:
    plan = await make_plan(task_rig)
    task_rig.llm.script(write_fix(FIXED), text_turn("done"))
    task_rig.sandbox.script(ScriptedResult(exit_code=0, stdout="1 passed"))

    await run(task_rig, plan)

    kinds = [e.kind for e in task_rig.session.committed_events]
    assert EventKind.CONSTRAINT_SNAPSHOT not in kinds  # not taken in this path
    assert EventKind.TURN_START in kinds and EventKind.TURN_END in kinds
    assert EventKind.TASK_CHECK in kinds
    states = [
        e.data["state"] for e in task_rig.session.committed_events if e.kind == EventKind.TASK_STATE
    ]
    assert states == ["running", "delivered"]


# ---------------------------------------------------------------------------
# The repair loop
# ---------------------------------------------------------------------------
async def test_a_failing_check_sends_the_model_back_to_fix_it(task_rig) -> None:
    plan = await make_plan(task_rig)
    task_rig.llm.script(
        write_fix(BUGGY),          # a fix that does not work
        text_turn("tried"),
        write_fix(FIXED),          # the real fix
        text_turn("done"),
    )
    task_rig.sandbox.script(
        ScriptedResult(exit_code=1, stdout="1 failed: retry loop is unbounded"),
        ScriptedResult(exit_code=0, stdout="1 passed"),
    )

    outcome = await run(task_rig, plan)

    assert outcome.delivered
    assert outcome.attempts == 2
    assert outcome.turns == 2
    assert [c.passed for c in outcome.checks] == [False, True]


async def test_the_retry_bound_is_respected(task_rig) -> None:
    """Bounded by the project's policy, never looping until it works."""
    plan = await make_plan(task_rig)
    for _ in range(5):
        task_rig.llm.script(write_fix(BUGGY), text_turn("tried again"))
    task_rig.sandbox.script(*[ScriptedResult(exit_code=1, stdout="still failing")] * 5)

    outcome = await run(task_rig, plan)

    assert outcome.state.value == "failed"
    assert outcome.attempts == task_rig.project.retry.max_attempts + 1
    assert task_rig.vcs.pull_requests == (), "nothing is delivered when checks fail"


async def test_task_mode_honours_the_configured_step_ceiling(task_rig) -> None:
    """⚠️ **`run_task` drives `run_turn` directly, so it never saw `max_steps`.**

    It runs the whole repair loop, and each attempt is a turn — so the real bound
    on a task is `attempts × max_steps`, and the configured ceiling reached only
    the chat path. A profile with `max_steps: 3` got 12 per attempt here.

    The count is asserted **exactly**, not bounded: the old test in the loop suite
    wrote `<= max_steps`, which passes whether the ceiling yields 3 steps or 2 —
    and it yields 2. See `test_the_step_ceiling_is_the_number_of_steps`.
    """
    from forgepilot.seams.session import EventKind

    plan = await make_plan(task_rig)
    for index in range(10):
        task_rig.llm.script(
            tool_call_turn(
                "read_file",
                json.dumps({"path": "src/login.py"}),
                call_id=f"c{index}",
            )
        )

    await run(task_rig, plan, max_steps=4)

    steps = sum(1 for e in task_rig.session.committed_events if e.kind == EventKind.STEP_START)
    assert steps == 4, f"{steps} step(s) ran against a ceiling of 4"


async def test_a_check_that_cannot_run_is_never_reported_as_a_pass(task_rig, constraints_factory) -> None:
    """Reporting success for checks nobody ran would claim verification that
    did not happen."""
    # Write is needed to create the branch; run_tests is the one withheld, so
    # the check is what fails rather than the setup.
    read_only = constraints_factory(Operation.READ_CODE, Operation.WRITE_CODE, repos=(REPO,))
    plan = await make_plan(task_rig, constraints=read_only)

    task_rig.llm.script(write_fix(FIXED), text_turn("done"))

    outcome = await run(task_rig, plan, constraints=read_only)

    assert outcome.state.value == "failed"
    assert outcome.checks[-1].could_not_run is True
    assert "not a pass" in outcome.checks[-1].output
    assert task_rig.vcs.pull_requests == ()


async def test_no_pull_request_is_opened_when_a_check_could_not_run(task_rig, constraints_factory) -> None:
    read_only = constraints_factory(Operation.READ_CODE, Operation.WRITE_CODE, repos=(REPO,))
    plan = await make_plan(task_rig, constraints=read_only)
    task_rig.llm.script(write_fix(FIXED), text_turn("done"))

    await run(task_rig, plan, constraints=read_only)

    assert task_rig.vcs.pull_requests == ()


# ---------------------------------------------------------------------------
# The tool set is enforced, not merely advertised
# ---------------------------------------------------------------------------
async def test_a_tool_outside_the_selection_is_refused_at_execution(task_rig) -> None:
    """Restricting the schemas is not enough: a model can name what it was never offered.

    The selection is narrowed through the *project configuration*, which is the
    real path — the runner sets the pipeline's allow-list from the plan, so
    mutating the pipeline by hand would be overwritten and would prove nothing.
    """
    from forgepilot.config.project import ProjectConfig

    narrow = ProjectConfig(
        name="svc-a",
        repos=(REPO,),
        default_branch="main",
        test=("pytest -q",),
        allowed_operations=frozenset(Operation),
        writable_paths=("src",),
        tool_sets={"default": ("write_file", "run")},  # read_file is NOT offered
    )
    plan = await plan_task(
        ticket=a_ticket(),
        project=narrow,
        constraints=task_rig.constraints,
        registry=task_rig.registry,
        classifier=ClassifierGateway(
            FixedClassifier(TicketUnderstanding(category="bug", confidence=0.95))
        ),
    )

    task_rig.llm.script(
        tool_call_turn("read_file", json.dumps({"path": "src/login.py"}), call_id="c9"),
        write_fix(FIXED),
        text_turn("done"),
    )
    task_rig.sandbox.script(ScriptedResult(exit_code=0, stdout="ok"))

    await run_task(
        plan=plan,
        project=narrow,
        session=task_rig.session,
        llm=task_rig.llm,
        tools=task_rig.registry,
        vcs=task_rig.vcs,
        workspace=task_rig.workspace,
        sandbox=task_rig.sandbox,
        constraints=task_rig.constraints,
        pipeline=task_rig.pipeline,
        tracker=task_rig.tracker,
        model="replay-1",
        policy=task_rig.policy,
        approval=task_rig.approval,
        principal=task_rig.principal,
    )

    results = [e for e in task_rig.session.committed_events if e.kind == EventKind.TOOL_RESULT]
    assert results, "the refusal is recorded so the model can see it"
    assert any("not part of this task's tool set" in str(r.data.get("content")) for r in results)


async def test_the_model_is_never_given_a_tool_that_opens_pull_requests(task_rig) -> None:
    """Delivery is harness-driven. A capability that exists is one that can be induced."""
    plan = await make_plan(task_rig)
    offered = {schema.name for schema in plan.tool_schemas(task_rig.registry)}

    assert "open_pr" not in offered
    assert task_rig.registry.get("open_pr") is None, (
        "no pull-request tool is registered at all; the harness opens the PR once "
        "the checks pass"
    )


# ---------------------------------------------------------------------------
# Well-formedness of the harness's own check
# ---------------------------------------------------------------------------
async def test_a_harness_check_reaches_the_model_as_a_user_message(task_rig) -> None:
    """Not as a tool result the model never asked for.

    A `tool/result` with no preceding assistant tool call is an orphan, and
    endpoints reject the whole request over it — the failure would look like a
    provider problem rather than a logging bug.
    """
    plan = await make_plan(task_rig)
    task_rig.llm.script(write_fix(FIXED), text_turn("done"))
    task_rig.sandbox.script(ScriptedResult(exit_code=0, stdout="1 passed"))

    await run(task_rig, plan)

    messages = derive_messages(task_rig.session.events)
    check_message = next(
        (m for m in messages if m.content and "harness ran this project's checks" in m.content),
        None,
    )
    assert check_message is not None
    assert check_message.role is Role.USER
    assert "pytest -q" in check_message.content
    assert "1 passed" in check_message.content


def _orphan_tool_messages(messages) -> list[str]:
    """Tool messages whose call id no assistant message ever requested."""
    requested: set[str] = set()
    orphans: list[str] = []
    for message in messages:
        for call in message.tool_calls:
            requested.add(call.call_id)
        if message.role is Role.TOOL and message.tool_call_id not in requested:
            orphans.append(str(message.tool_call_id))
    return orphans


async def test_the_conversation_stays_well_formed_across_the_whole_task(task_rig) -> None:
    plan = await make_plan(task_rig)
    task_rig.llm.script(
        write_fix(BUGGY), text_turn("tried"),
        write_fix(FIXED), text_turn("done"),
    )
    task_rig.sandbox.script(
        ScriptedResult(exit_code=1, stdout="failing"),
        ScriptedResult(exit_code=0, stdout="passing"),
    )

    await run(task_rig, plan)

    messages = derive_messages(task_rig.session.events)

    assert _orphan_tool_messages(messages) == [], (
        "every tool result must answer a tool call the model made"
    )
    assert all(m.source_event_seq is not None for m in messages)


# ---------------------------------------------------------------------------
# Planning is reused, not bypassed
# ---------------------------------------------------------------------------
async def test_a_ticket_that_cannot_be_scoped_is_never_executed(task_rig) -> None:
    """No repository, nothing to run against — and no model call to guess one."""
    from forgepilot.config.project import ProjectConfig

    project = ProjectConfig(name="scopeless", tool_sets={"default": ("read_file",)})
    plan = await plan_task(
        ticket=a_ticket(),
        project=project,
        constraints=task_rig.constraints,
        registry=task_rig.registry,
        classifier=ClassifierGateway(
            FixedClassifier(TicketUnderstanding(category="bug", confidence=0.99))
        ),
    )

    assert plan.needs_human
    assert "no repository" in plan.reason

    outcome = await run(task_rig, plan)
    assert not outcome.delivered
    assert task_rig.vcs.pull_requests == ()


# ---------------------------------------------------------------------------
# No repo host: stop at the draft, and say so
# ---------------------------------------------------------------------------
class NoHostVcs:
    """Real for everything local; cannot publish.

    A repo host is the service that *owns* pull requests. Without one there is
    nothing to open a PR against, and inventing a reference would be the
    mock-that-lies failure this project keeps hitting.

    It says so through `capabilities()` — the seam's own mechanism for
    declaring what a provider can do, which until now nothing read.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.opened: list = []

    def capabilities(self) -> frozenset[str]:
        return self._inner.capabilities() - {"pull_requests"}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def open_pull_request(self, draft, constraints):
        self.opened.append(draft)
        raise AssertionError(
            "open_pull_request was called on a provider that declares it cannot "
            "publish"
        )


async def test_without_a_repo_host_the_change_stops_as_a_ready_draft(task_rig) -> None:
    """Publishing needs a service that owns pull requests. Absent one, the
    harness's job ends — with the work done and the draft written, not with a
    failure and not with a fabricated PR.

    The document's own demo milestone says the deliverable is a "可评审 PR
    草稿" — a draft. The seam models that (`PullRequestDraft.draft`), and the
    code sets the flag and then opens it anyway, which is what turned an
    optional publish step into a hard dependency on a service that does not
    exist here.
    """
    from forgepilot.orchestration.task_state import TaskState

    vcs = NoHostVcs(task_rig.vcs)
    task_rig.vcs = vcs
    plan = await make_plan(task_rig)
    task_rig.llm.script(write_fix(FIXED), text_turn('done'))

    outcome = await run(task_rig, plan)

    assert vcs.opened == [], "a provider that cannot publish was asked to publish"
    assert outcome.state is TaskState.READY
    assert outcome.state is not TaskState.DELIVERED, (
        "nothing was published, so claiming delivery is the same lie the mock refused to tell"
    )
    assert outcome.pull_request is None
    assert outcome.draft is not None, 'the draft is the deliverable here'
    assert outcome.draft.source_branch == outcome.branch
    assert outcome.draft.title.startswith("fix:")
    assert "repo host" in outcome.reason, outcome.reason


async def test_the_draft_carries_the_evidence_the_run_produced(task_rig) -> None:
    """The draft is not a placeholder — it is built from the real checks and the
    real diff, so a human can act on it without rerunning anything.
    """
    vcs = NoHostVcs(task_rig.vcs)
    task_rig.vcs = vcs
    plan = await make_plan(task_rig)
    task_rig.llm.script(write_fix(FIXED), text_turn('done'))

    outcome = await run(task_rig, plan)

    assert "T-1" in outcome.draft.body_md, 'not traceable to the ticket'
    assert outcome.draft.body_md.strip(), 'an empty PR body is not reviewable'


async def test_the_trace_says_the_change_was_not_published(task_rig) -> None:
    """An operator reading the session must not have to infer it."""
    from forgepilot.orchestration.task_state import TaskState

    task_rig.vcs = NoHostVcs(task_rig.vcs)
    plan = await make_plan(task_rig)
    task_rig.llm.script(write_fix(FIXED), text_turn('done'))

    outcome = await run(task_rig, plan)

    states = [e for e in task_rig.session.events if e.kind == EventKind.TASK_STATE]
    final = states[-1].data
    assert final['state'] == TaskState.READY.value
    assert not final.get('pullRequest'), 'a PR was reported that was never opened'


# ---------------------------------------------------------------------------
# Publishing is the harness's own outward-facing act
# ---------------------------------------------------------------------------
class AsksForPublishing:
    """A policy that escalates publishing to a human and allows everything else.

    A stand-in rather than the real `PolicyServicePlugin`, because what is under
    test here is **`run_task`'s handling** of a decision — the service itself, and
    the shipped config that drives it, are covered in
    `tests/identity/test_policy_is_wired.py`. `run_task` needs nothing more than
    an object with `decide`, and saying so with a two-line fake is clearer than
    reconfiguring a rig.
    """

    def decide(self, principal, operation, resource=None):  # noqa: ANN001
        from forgepilot.identity.policy import PolicyDecision

        if operation is Operation.OPEN_PR:
            return PolicyDecision.ASK
        return PolicyDecision.ALLOW


async def test_publishing_that_needs_a_human_and_gets_none_stops_at_ready(task_rig) -> None:
    """**The claim that used to be false.**

    `ask_operations: [open_pr]` was documented as making a task stop and ask
    before it published. It did not: opening a pull request is not a tool call,
    so no pre-execute listener could ever see it, and `run_task` called the seam
    directly.

    With no answerer registered, an `ASK` resolves to `UNAVAILABLE` — and
    everything except `ALLOWED_ONCE` refuses. So the run ends at `READY`, which is
    exactly what `READY` is for: checks passed, work committed, **nothing
    published**, and the draft goes back with the outcome so a person can raise it
    by hand.
    """
    from forgepilot.seams.approval import ApprovalOutcome

    task_rig.llm.script(
        tool_call_turn("write_file", json.dumps({"path": "src/login.py", "content": FIXED}), call_id="c1"),
        text_turn("修好了"),
    )
    task_rig.sandbox.script(ScriptedResult(exit_code=0, stdout="1 passed"))

    plan = await make_plan(task_rig)
    task_rig.approval.set_answerer(lambda request: ApprovalOutcome.UNAVAILABLE)

    outcome = await run(
        task_rig, plan, policy=AsksForPublishing(), principal=task_rig.principal
    )

    from forgepilot.orchestration.task_state import TaskState

    assert outcome.state is TaskState.READY, outcome.reason
    assert not outcome.delivered
    # **Nothing went out.** Asserted on the pushes as well as the pull requests:
    # a branch that reached the host is published whether or not a PR followed.
    assert task_rig.vcs.pushes == (), "a branch was pushed without approval"
    assert task_rig.vcs.pull_requests == (), "a pull request was opened without approval"
    # And the work is not lost — it is a draft somebody can raise.
    assert outcome.draft is not None
    assert outcome.checks[-1].passed is True


async def test_publishing_proceeds_once_a_human_allows_it(task_rig) -> None:
    """The other direction, so the test above cannot pass by refusing everything."""
    from forgepilot.seams.approval import ApprovalOutcome

    task_rig.llm.script(
        tool_call_turn("write_file", json.dumps({"path": "src/login.py", "content": FIXED}), call_id="c1"),
        text_turn("修好了"),
    )
    task_rig.sandbox.script(ScriptedResult(exit_code=0, stdout="1 passed"))

    plan = await make_plan(task_rig)
    asked: list[str] = []

    def answer(request):
        asked.append(request.tool_name)
        return ApprovalOutcome.ALLOWED_ONCE

    task_rig.approval.set_answerer(answer)

    outcome = await run(
        task_rig, plan, policy=AsksForPublishing(), principal=task_rig.principal
    )

    assert outcome.delivered, outcome.reason
    assert asked == ["harness:open_pr"], "the human was not asked, or was asked something else"
    assert task_rig.vcs.pushes, "the branch was never pushed"
    assert task_rig.vcs.pull_requests
    # The prompt has to be in the log, or a human never learns there is one.
    kinds = [str(e.kind) for e in task_rig.session.committed_events]
    assert EventKind.APPROVAL_ASKED in kinds


# ---------------------------------------------------------------------------
# Both trees, and both have to pass
# ---------------------------------------------------------------------------
def test_combine_checks_is_a_conjunction() -> None:
    """Neither run alone is enough, and the reason is symmetric.

    Pinned alone cannot see a feature — restoring the tests to base deletes the
    specification for something that does not exist yet. Agent alone cannot see a
    weakened assertion. Reporting the conjunction is what makes both visible.
    """
    from forgepilot.runtime.repair import CheckResult, combine_checks

    ok = CheckResult(passed=True, command="pytest", output="2 passed")
    bad = CheckResult(passed=False, command="pytest", output="1 failed", exit_code=1)

    assert combine_checks((("pinned", ok), ("agent", ok))).passed
    assert not combine_checks((("pinned", ok), ("agent", bad))).passed
    assert not combine_checks((("pinned", bad), ("agent", ok))).passed

    both = combine_checks((("pinned", ok), ("agent", bad)))
    assert "pinned" in both.output and "agent" in both.output, (
        "a reader cannot tell which tree failed"
    )
    assert both.meta["runs"]["agent"]["passed"] is False


def test_combine_checks_keeps_an_answer_when_one_run_could_not_run() -> None:
    """If one tree was actually checked, that answer is evidence.

    Reporting `could_not_run` over it would discard the only result there is —
    and a check that cannot run is never a pass, so the failure is still a
    failure either way.
    """
    from forgepilot.runtime.repair import CheckResult, combine_checks

    ran = CheckResult(passed=True, command="pytest", output="2 passed")
    did_not = CheckResult(passed=False, could_not_run=True, reason="no sandbox")

    combined = combine_checks((("pinned", did_not), ("agent", ran)))

    assert combined.could_not_run is False
    assert combined.passed is False, "a check that could not run was treated as a pass"


async def test_changed_tests_are_checked_against_both_trees(task_rig) -> None:
    """The wiring: a changed test path means the project's command runs twice.

    Asserted on the number of times the sandbox was asked to run, because that is
    the thing that would silently stop happening.
    """
    task_rig.llm.script(
        tool_call_turn(
            "write_file",
            json.dumps({"path": "tests/test_login.py", "content": "def test_retry_is_bounded():\n    pass\n"}),
            call_id="c1",
        ),
        text_turn("改了测试"),
        text_turn("还是不行"),
        text_turn("放弃"),
    )
    task_rig.sandbox.script(
        ScriptedResult(exit_code=1, stdout="1 failed"),
        ScriptedResult(exit_code=0, stdout="1 passed"),
        ScriptedResult(exit_code=1, stdout="1 failed"),
        ScriptedResult(exit_code=1, stdout="1 failed"),
    )

    plan = await make_plan(task_rig)
    outcome = await run(task_rig, plan)

    runs = task_rig.sandbox.calls if hasattr(task_rig.sandbox, "calls") else []
    assert len(runs) >= 2, (
        "the checks ran only once, so a changed test was not checked against both "
        "the pinned tree and the agent's"
    )
    assert not outcome.delivered, "a run whose agent-tree check failed was delivered"
