"""End to end: the task mode's own pull request, reviewed by the review mode.

This is the acceptance criterion — a real diff produced by M8, fed to M9, coming
out as a narrative whose **every claim traces to that session's log**.

It is also the case the evidence design was built for. Reviewing our own PR means
the evidence is real: which tool call touched which file, which check ran and
what it returned, what the model was thinking. An external PR would leave the
narrative citing only code locations, and "this was tested" would be unprovable
rather than merely unproven.
"""

from __future__ import annotations

import json

from forgepilot.orchestration import ClassifierGateway
from forgepilot.orchestration.modes.review import build_review, prepare_review
from forgepilot.orchestration.modes.task import plan_task, run_task
from forgepilot.providers.llm.replay import text_turn, tool_call_turn
from forgepilot.providers.sandbox.scripted import ScriptedResult
from forgepilot.runtime.review import (
    EvidenceUniverse,
    render_json,
    render_markdown,
)
from forgepilot.seams.classifier import TicketUnderstanding
from forgepilot.seams.refs import RepoRef, TicketId
from forgepilot.seams.tickets import Ticket, TicketPriority

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")

BUGGY = (
    "MAX_ATTEMPTS = 3\n\n\ndef retry_login(client, credentials):\n"
    "    attempts = 0\n    while True:\n        try:\n"
    "            return client.login(credentials)\n        except AuthError:\n"
    "            attempts += 1\n"
)
FIXED = (
    "MAX_ATTEMPTS = 3\n\n\ndef retry_login(client, credentials):\n"
    "    for _ in range(MAX_ATTEMPTS):\n        try:\n"
    "            return client.login(credentials)\n        except AuthError:\n"
    "            continue\n    raise RuntimeError('login failed after retries')\n"
)


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
        description_md="复现步骤：\n1. 输错密码\n\n预期结果：提示密码错误\n实际结果：无限重试",
        priority=TicketPriority.HIGH,
    )


async def deliver_a_fix(rig):
    """Run M8 to produce a real diff, with a failing check followed by a passing one."""
    plan = await plan_task(
        ticket=a_ticket(),
        project=rig.project,
        constraints=rig.constraints,
        registry=rig.registry,
        classifier=ClassifierGateway(
            FixedClassifier(TicketUnderstanding(category="bug", symptom="无限重试", confidence=0.95))
        ),
    )
    rig.llm.script(
        tool_call_turn("write_file", json.dumps({"path": "src/login.py", "content": BUGGY}), call_id="c1"),
        text_turn("试了一版没有上限的修复"),
        tool_call_turn("write_file", json.dumps({"path": "src/login.py", "content": FIXED}), call_id="c2"),
        text_turn("给重试循环加了上限，超限后抛错"),
    )
    rig.sandbox.script(
        ScriptedResult(exit_code=1, stdout="1 failed: retry loop is unbounded"),
        ScriptedResult(exit_code=0, stdout="1 passed"),
    )
    outcome = await run_task(
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
    )
    assert outcome.delivered, outcome.reason
    return outcome


# ---------------------------------------------------------------------------
# The acceptance criterion
# ---------------------------------------------------------------------------
async def test_a_task_pr_becomes_a_narrative_whose_claims_trace_to_the_log(task_rig) -> None:
    outcome = await deliver_a_fix(task_rig)

    narrative = await build_review(
        outcome.diff,
        title=f"fix: {a_ticket().title}",
        session=task_rig.session,
        llm=None,
        use_model=False,
        ticket_id="T-1",
    )

    assert narrative.groups, "the diff has at least one group"
    assert narrative.verified_claims, "the deterministic narrative verifies"

    # Every citation resolves against this session and this diff.
    universe = EvidenceUniverse.from_session(
        task_rig.session,
        changed_paths=[f.path for f in prepare_review(outcome.diff).parsed.files],
        hunk_ids=[h.hunk_id for h in prepare_review(outcome.diff).parsed.hunks],
    )
    for claim in narrative.verified_claims:
        assert claim.evidence, f"verified without evidence: {claim.text!r}"
        for ref in claim.evidence:
            assert universe.resolve(ref) == "", f"{claim.text!r} cites {ref.describe()}"


async def test_the_narrative_names_the_check_that_actually_ran(task_rig) -> None:
    """The evidence a session log provides and a bare diff cannot."""
    outcome = await deliver_a_fix(task_rig)

    payload = render_json(
        await build_review(
            outcome.diff, title="t", session=task_rig.session, use_model=False
        )
    )

    kinds = {ref["kind"] for ref in payload["evidence"]}
    assert "diff_hunk" in kinds
    assert payload["sessionId"], "the narrative names the session it is about"


async def test_the_rendered_document_shows_order_coverage_and_what_is_unverified(task_rig) -> None:
    outcome = await deliver_a_fix(task_rig)
    narrative = await build_review(
        outcome.diff, title="fix: 登录无限重试", session=task_rig.session, use_model=False
    )

    markdown = render_markdown(narrative)

    assert "## 建议的阅读顺序" in markdown
    assert "## 覆盖情况" in markdown
    assert "## 未能核实" in markdown
    assert "不是按文件名" in markdown, "the order is deliberate and says so"
    # A text-backend analysis must announce itself rather than pass as an AST one.
    assert "文本匹配" in markdown or "AST" in markdown


# ---------------------------------------------------------------------------
# The model path, and what happens when it cites something invented
# ---------------------------------------------------------------------------
async def test_a_model_authored_claim_with_a_real_citation_is_verified(task_rig) -> None:
    outcome = await deliver_a_fix(task_rig)
    plan = prepare_review(outcome.diff)
    real_hunk = plan.parsed.hunks[0].hunk_id
    real_seq = task_rig.session.committed_events[0].seq

    task_rig.llm.script(
        text_turn(
            json.dumps(
                {
                    "intent": {
                        "text": "把无上限的重试改成有界循环。",
                        "cites": [{"kind": "diff_hunk", "hunk_id": real_hunk}],
                    },
                    "approach": [
                        {
                            "text": "重试次数有了上限。",
                            "cites": [{"kind": "session_event", "session_seq": real_seq}],
                        }
                    ],
                    "risks": [],
                }
            )
        )
    )

    narrative = await build_review(
        outcome.diff,
        title="t",
        session=task_rig.session,
        llm=task_rig.llm,
        model="replay-1",
    )

    assert narrative.meta["generator"] == "model"
    assert narrative.intent.verified
    assert "有界循环" in narrative.intent.text


async def test_a_model_authored_claim_citing_an_invented_hunk_is_demoted(task_rig) -> None:
    """The failure this whole design is arranged to catch.

    A fluent, plausible, *wrong* narrative is the dangerous kind — it reads as
    authoritative. Validation is what turns it into a listed uncertainty instead.
    """
    outcome = await deliver_a_fix(task_rig)

    task_rig.llm.script(
        text_turn(
            json.dumps(
                {
                    "intent": {
                        "text": "重构了整个认证子系统，顺带修复了竞态条件。",
                        "cites": [{"kind": "diff_hunk", "hunk_id": "src/imaginary.py#999"}],
                    },
                    "approach": [
                        {"text": "引入了分布式锁。", "cites": [{"kind": "session_event", "session_seq": 999999}]},
                        {"text": "性能提升三倍。"},  # no citation at all
                    ],
                    "risks": [],
                }
            )
        )
    )

    narrative = await build_review(
        outcome.diff, title="t", session=task_rig.session, llm=task_rig.llm, model="replay-1"
    )

    assert not narrative.intent.verified
    assert "not in this diff" in narrative.intent.unverified_reason
    assert len(narrative.unverified) >= 2, "both the invented and the uncited claim"

    markdown = render_markdown(narrative)
    assert "重构了整个认证子系统" in markdown
    assert "原因：" in markdown, "each demoted claim says why"


async def test_a_model_that_returns_garbage_degrades_to_the_deterministic_narrative(task_rig) -> None:
    """A model failure must not fail the review — the deterministic floor holds."""
    outcome = await deliver_a_fix(task_rig)
    task_rig.llm.script(text_turn("I'm afraid I can't do that."))

    narrative = await build_review(
        outcome.diff, title="t", session=task_rig.session, llm=task_rig.llm, model="replay-1"
    )

    assert narrative.meta["generator"] == "deterministic"
    assert narrative.verified_claims, "still a usable, checkable document"
