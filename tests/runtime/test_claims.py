"""Every claim a pull request makes has to be an event in the log.

These tests are about the *pointer*, not the prose: `claims_for` produces the sentences
and their `seq`, `render_evidence` prints them, and the body builder appends the section
only when there is something to cite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from forgepilot.runtime.claims import Claim, claims_for, render_evidence


@dataclass(frozen=True)
class FakeEvent:
    seq: int
    kind: str
    data: dict = field(default_factory=dict)


def check(seq: int, *, repo: str | None = "git.local/demo/app", **overrides):
    data = {"repo": repo, "passed": True, "command": "pytest -q", "exitCode": 0, "meta": {}}
    data.update(overrides)
    return FakeEvent(seq, "task/check", data)


def state(seq: int, *, repo: str | None = "git.local/demo/app", **overrides):
    data: dict = {"state": "delivered"}
    if repo is not None:
        data["repo"] = repo
    data.update(overrides)
    return FakeEvent(seq, "task/state", data)


REPO = "git.local/demo/app"


def test_a_check_event_becomes_a_claim_that_cites_it():
    claims = claims_for([check(7, command="pytest -q")], repo=REPO)
    assert claims == (Claim("`pytest -q` — passed", "task/check", 7),)


def test_a_failed_check_says_failed_and_its_exit_code():
    claims = claims_for([check(9, passed=False, exitCode=2)], repo=REPO)
    assert claims[0].text == "`pytest -q` — FAILED (exit 2)"


def test_a_flaky_check_carries_the_attempt():
    claims = claims_for([check(11, passed=True, meta={"flaky": True, "attempts": 2})], repo=REPO)
    assert claims[0].text == "`pytest -q` — passed on attempt 2 (flaky: the first run failed)"


def test_the_delivery_state_gives_the_commit_and_the_changed_count():
    claims = claims_for([state(20, commit="abc1234", changed=["a.py", "b.py"])], repo=REPO)
    assert [claim.text for claim in claims] == ["commit `abc1234`", "2 path(s) changed"]
    assert {claim.seq for claim in claims} == {20}


def test_the_verdict_comes_from_the_aggregate_state():
    claims = claims_for([state(31, repo=None, state="delivered")], repo=REPO)
    assert claims == (Claim("verdict `delivered`", "task/state", 31),)


def test_another_repository_is_not_credited():
    events = [
        check(1, repo="git.local/demo/other"),
        state(2, repo="git.local/demo/other", commit="deadbee", changed=["x.py"]),
        check(3),
    ]
    claims = claims_for(events, repo=REPO)
    assert [claim.seq for claim in claims] == [3]


def test_an_event_without_a_repo_belongs_to_the_single_repository_run():
    claims = claims_for([check(4, repo=None)], repo=REPO)
    assert claims[0].seq == 4


def test_no_events_means_no_evidence_section():
    assert claims_for([], repo=REPO) == ()
    assert render_evidence("s-1", ()) == []


def test_the_section_names_the_session_and_each_seq():
    lines = render_evidence("s-2026", claims_for([check(5)], repo=REPO))
    joined = "\n".join(lines)
    assert lines[0] == ""
    assert "## Evidence" in joined
    assert "`s-2026`" in joined
    assert "GET /sessions/s-2026/events" in joined
    assert "event 5 (`task/check`)" in joined
