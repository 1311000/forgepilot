"""P27: did the index answer for the revision that was delivered?

The log has held both halves since M14 and M18 — `meta.ref` on every `read_code` result, and
the delivered commit on `task/state` — and nothing read them together, so "was the code that
was read the code that changed?" was answerable only by re-reading a transcript.

Four properties are what these tests are for, and each is a way to get the answer wrong:

* a stale read is **mixed**, not consistent: the index answered, and it answered for a tree
  nobody delivered;
* the same commit spelled two ways (full sha, or git's abbreviation) is **consistent** — that
  is git's own rule, not a special case invented here;
* a branch name is **not** a revision statement, so it never counts as agreement;
* silence is **unknown**, never consistent — no retrieval, or a session that delivered nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forgepilot.runtime.index_audit import (
    CONSISTENT,
    MIXED,
    UNKNOWN,
    RepoAudit,
    audit,
    same_revision,
)

A = "a" * 40
B = "b" * 40
API = "git.corp/team/api"
WEB = "git.corp/team/web"


@dataclass
class Event:
    kind: str
    data: dict[str, Any]


def a_read(repo: str, ref: str, *, stale: bool = False) -> Event:
    meta: dict[str, Any] = {"repo": repo, "path": "limits.py", "ref": ref, "start": 1, "end": 9}
    if stale:
        meta["stale"] = True
        meta["workingRef"] = B
    return Event("tool/result", {"callId": "c", "meta": meta, "content": "x"})


def a_search(*repos: str, stale: tuple[str, ...] = ()) -> Event:
    return Event(
        "tool/result",
        {
            "callId": "c",
            "meta": {"repos": list(repos), "staleRepos": list(stale), "hits": 1},
            "content": "y",
        },
    )


def a_delivery(repo: str, commit: str) -> Event:
    return Event("task/state", {"repo": repo, "state": "delivered", "commit": commit})


def test_a_read_from_the_delivered_revision_is_consistent() -> None:
    report = audit([a_read(API, A), a_search(API), a_delivery(API, A)])
    assert report.verdict == CONSISTENT
    assert report.repos[0].served == (A,)
    assert report.repos[0].searches == 1


def test_a_read_from_another_revision_is_mixed_and_names_both() -> None:
    report = audit([a_read(API, B), a_delivery(API, A)])
    assert report.verdict == MIXED
    assert B[:12] in report.reason
    assert A[:12] in report.reason


def test_a_read_that_itself_said_stale_is_mixed_even_at_the_delivered_sha() -> None:
    """The ref can match and the call can still have been behind: both facts are in the log."""
    report = audit([a_read(API, A, stale=True), a_delivery(API, A)])
    assert report.verdict == MIXED
    assert "behind the checkout" in report.reason
    assert report.repos[0].stale_reads == 1


def test_a_stale_search_counts_too() -> None:
    report = audit([a_read(API, A), a_search(API, stale=(API,)), a_delivery(API, A)])
    assert report.verdict == MIXED
    assert report.repos[0].stale_searches == 1


def test_an_abbreviated_revision_matches_the_full_one() -> None:
    """git prints 12 characters; the log holds 40. That is agreement, not disagreement."""
    report = audit([a_read(API, A[:12]), a_delivery(API, A)])
    assert report.verdict == CONSISTENT
    assert same_revision(A[:12], A)


def test_a_branch_name_never_counts_as_agreement() -> None:
    """`main` cannot be shown to be the delivered commit without resolving it, so it cannot."""
    assert not same_revision("main", A)
    report = audit([a_read(API, "main"), a_delivery(API, A)])
    assert report.verdict == MIXED


def test_searches_alone_are_unknown_because_they_carry_no_revision() -> None:
    report = audit([a_search(API), a_delivery(API, A)])
    assert report.verdict == UNKNOWN
    assert "no retrieval answered" in report.reason
    assert report.repos[0].searches == 1


def test_a_read_without_a_delivery_is_unknown_and_says_which_repository() -> None:
    report = audit([a_read(API, A)])
    assert report.verdict == UNKNOWN
    assert API in report.reason
    assert report.answered[0].repo == API


def test_each_repository_is_judged_on_its_own() -> None:
    report = audit(
        [
            a_read(API, A),
            a_read(WEB, B),
            a_delivery(API, A),
            a_delivery(WEB, A),
        ]
    )
    assert report.verdict == MIXED
    rows: dict[str, RepoAudit] = {row.repo: row for row in report.repos}
    assert rows[API].delivered == A
    assert rows[WEB].served == (B,)
    assert WEB in report.reason and API not in report.reason
