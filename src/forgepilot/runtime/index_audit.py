"""Did the index answer for the revision that was delivered?

A session log holds, per retrieval tool call, the revision the index served (`meta.ref`) and
— since P25 — the revision the checkout held (`meta.workingRef`, `meta.stale`). It also holds
the commit each delivery produced. Nothing read those together, so the question a reviewer
actually has was unanswerable from the durable record: *was the code that was read the code
that was changed?*

This module answers it as a pure function of events. Three verdicts, and the third one is the
point:

* ``consistent`` — every repository that answered has exactly one served revision, no call
  reported itself stale, and that revision is the one that was delivered (a full sha, or a
  prefix of it: git's own abbreviation rule);
* ``mixed`` — some answer came from a revision that was not delivered, or a call said the
  index lagged the checkout. **A mixed session is not a failure** — an index that updates
  mid-session is a normal afternoon — but it is a fact a reviewer should be told rather than
  left to infer from prose in a pull-request body;
* ``unknown`` — nothing to compare. No retrieval answered, or the session delivered nothing
  (chat, review, an abandoned task). This must never be read as ``consistent``: silence is
  not agreement, which is the same rule `--check` follows when git cannot place a revision.

Deliberately silent about `code_search`: its result records the repositories it searched and
which of them were stale, but not a per-hit revision, so it contributes ``searches`` and
``stale_searches`` and never a served ref. Counting it as an answer would be inventing
precision the log does not have.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

CONSISTENT = "consistent"
MIXED = "mixed"
UNKNOWN = "unknown"

TOOL_RESULT = "tool/result"
TASK_STATE = "task/state"


@dataclass(frozen=True, slots=True)
class RepoAudit:
    repo: str
    served: tuple[str, ...]
    reads: int
    stale_reads: int
    searches: int
    stale_searches: int
    delivered: str | None


@dataclass(frozen=True, slots=True)
class AuditReport:
    repos: tuple[RepoAudit, ...]
    verdict: str
    reason: str

    @property
    def answered(self) -> tuple[RepoAudit, ...]:
        return tuple(row for row in self.repos if row.served)


def _short(value: str) -> str:
    return value[:12]


def same_revision(served: str, delivered: str) -> bool:
    """Whether two revision strings can be compared at all, and agree.

    A full sha compares to itself; an abbreviated one compares by prefix, which is how git
    itself answers. A branch name is *not* a revision statement — `main` cannot be shown to
    be the delivered commit without resolving it — so it does not match anything here, and
    the caller sees ``mixed`` rather than a guess.
    """
    if served == delivered:
        return True
    left, right = served.strip(), delivered.strip()
    if len(left) < 7 or len(right) < 7:
        return False
    return left.startswith(right[:12]) or right.startswith(left[:12])


def _meta(event: Any) -> dict[str, Any] | None:
    data = getattr(event, "data", None)
    if not isinstance(data, dict):
        return None
    meta = data.get("meta")
    return meta if isinstance(meta, dict) else None


def audit(events: Any) -> AuditReport:
    """Fold a session's events into one verdict about index-versus-delivery agreement."""
    served: dict[str, list[str]] = defaultdict(list)
    reads: dict[str, int] = defaultdict(int)
    stale_reads: dict[str, int] = defaultdict(int)
    searches: dict[str, int] = defaultdict(int)
    stale_searches: dict[str, int] = defaultdict(int)
    delivered: dict[str, str] = {}
    seen: set[str] = set()

    for event in events:
        kind = getattr(event, "kind", None)
        data = getattr(event, "data", None)
        if not isinstance(data, dict):
            continue
        if kind == TASK_STATE:
            repo = data.get("repo")
            commit = data.get("commit")
            if isinstance(repo, str) and isinstance(commit, str) and commit:
                delivered[repo] = commit
            if isinstance(repo, str):
                seen.add(repo)
            continue
        if kind != TOOL_RESULT:
            continue
        meta = _meta(event)
        if meta is None:
            continue
        repos = meta.get("repos")
        if isinstance(repos, list):
            message = str(data.get("content") or "")
            stale = bool(meta.get("staleRepos")) or "behind the checkout" in message
            for repo in repos:
                if not isinstance(repo, str):
                    continue
                seen.add(repo)
                searches[repo] += 1
                if stale:
                    stale_searches[repo] += 1
            continue
        repo, ref = meta.get("repo"), meta.get("ref")
        if isinstance(repo, str) and isinstance(ref, str) and ref:
            seen.add(repo)
            reads[repo] += 1
            served[repo].append(ref)
            if meta.get("stale") is True:
                stale_reads[repo] += 1

    rows = tuple(
        RepoAudit(
            repo=repo,
            served=tuple(sorted(set(served.get(repo, ())))),
            reads=reads.get(repo, 0),
            stale_reads=stale_reads.get(repo, 0),
            searches=searches.get(repo, 0),
            stale_searches=stale_searches.get(repo, 0),
            delivered=delivered.get(repo),
        )
        for repo in sorted(seen)
    )

    answered = tuple(row for row in rows if row.served)
    if not answered:
        return AuditReport(
            rows,
            UNKNOWN,
            "no retrieval answered from an index in this session, so there is nothing to compare",
        )
    undelivered = [row.repo for row in answered if row.delivered is None]
    if undelivered:
        return AuditReport(
            rows,
            UNKNOWN,
            "this session delivered nothing for "
            + ", ".join(undelivered)
            + "; an answer cannot disagree with a delivery that does not exist",
        )

    mismatched: list[str] = []
    stale: list[str] = []
    for row in answered:
        assert row.delivered is not None
        if any(not same_revision(ref, row.delivered) for ref in row.served):
            mismatched.append(
                f"{row.repo} answered from {', '.join(_short(ref) for ref in row.served)} "
                f"but delivered {_short(row.delivered)}"
            )
        if row.stale_reads or row.stale_searches:
            stale.append(
                f"{row.repo} reported the index behind the checkout on "
                f"{row.stale_reads + row.stale_searches} call(s)"
            )
    problems = mismatched + stale
    if problems:
        return AuditReport(rows, MIXED, "; ".join(problems))
    return AuditReport(
        rows,
        CONSISTENT,
        "every answer came from the revision that was delivered, and none reported the index behind",
    )
