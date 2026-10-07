"""Deterministic ticket preprocessing. **No model is involved, ever.**

Everything here is rules over text: required fields, whether reproduction steps
and an expected result are present, which known repositories the ticket mentions,
and structured fragments like error codes and stack traces.

Two properties make this worth having as its own stage rather than folding it
into the classifier:

**It is auditable.** A rule can be read, tested and argued with. "The harness
decided this ticket was incomplete" is a claim someone can check; "the model
decided" is not.

**It bounds what the model is asked.** A ticket missing reproduction steps is
rejected here, so the classifier is never asked to interpret a report that cannot
be interpreted. It also means the answer to "does every request go through the
model?" is demonstrably no.

When the rules cannot tell, the result says **ask a human** — never a guess. A
guess here would be the worst kind: it would look like preprocessing, which
nobody thinks to double-check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from forgepilot.seams.refs import RepoRef
from forgepilot.seams.tickets import Ticket

__all__ = ["PreprocessResult", "preprocess", "extract_signals"]

#: Phrases that indicate the reporter described how to reproduce the problem.
#: Deliberately broad and multilingual: a rule that misses a real reproduction
#: section costs a round trip with a human, which is cheap; one that accepts a
#: ticket without steps is expensive.
_REPRO_MARKERS = (
    r"复现",
    r"重现",
    r"步骤",
    r"steps?\s+to\s+reproduce",
    r"how\s+to\s+reproduce",
    r"repro(?:duction)?\s+steps",
    r"^\s*\d+[\.、)]\s+\S",  # a numbered list, on its own line
)
_EXPECTED_MARKERS = (
    r"预期",
    r"期望",
    r"应该",
    r"expected\s+(?:result|behaviour|behavior)",
    r"\bshould\s+(?:be|return|show)",
)
_ACTUAL_MARKERS = (
    r"实际",
    r"实际上",
    r"actual(?:ly)?\s+(?:result|behaviour|behavior)",
    r"\bbut\s+(?:it\s+)?(?:returns?|shows?|does)",
)
_ERROR_CODE = re.compile(r"\b([A-Z]{2,}[A-Z0-9]*[-_]\d{3,})\b")
_STACK_LINE = re.compile(r'^\s*(?:at\s+\S+\(|File\s+"|\s+at\s+\S+:\d+)', re.MULTILINE)
_PATH = re.compile(r"\b((?:[\w.-]+/)+[\w.-]+\.\w{1,6})\b")


@dataclass(frozen=True, slots=True)
class PreprocessResult:
    """What the rules concluded. Descriptive, and never a permission."""

    ticket: Ticket
    missing: tuple[str, ...] = ()
    signals: Mapping[str, Any] = field(default_factory=dict)
    repos: tuple[RepoRef, ...] = ()
    reason: str = ""

    @property
    def needs_human(self) -> bool:
        """Whether a person must supply more information before anything runs."""
        return bool(self.missing)

    @property
    def ready(self) -> bool:
        return not self.needs_human


def _has_any(patterns: Sequence[str], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE | re.MULTILINE) for pattern in patterns)


def extract_signals(description: str, *, known_repos: Sequence[RepoRef] = ()) -> dict[str, Any]:
    """Pull structured fragments out of free text, using rules only."""
    codes = sorted(set(_ERROR_CODE.findall(description)))
    paths = sorted({p for p in _PATH.findall(description) if "/" in p})
    stack = bool(_STACK_LINE.search(description))

    # Only repositories the project already knows about are reported. Extracting
    # an arbitrary `owner/repo` from text would let a ticket name a repository
    # nobody has authorised — and the router would then think it was in scope.
    mentioned: list[RepoRef] = []
    for repo in known_repos:
        if repo.slug in description or repo.name in description:
            mentioned.append(repo)

    return {
        "error_codes": codes,
        "paths": paths[:20],
        "has_stack_trace": stack,
        "mentioned_repos": [r.slug for r in mentioned],
    }


def preprocess(
    ticket: Ticket,
    *,
    known_repos: Sequence[RepoRef] = (),
) -> PreprocessResult:
    """Validate and normalise ``ticket`` using rules alone."""
    missing: list[str] = []
    description = ticket.description_md or ""
    haystack = f"{ticket.title}\n{description}"

    if not ticket.title.strip():
        missing.append("title")
    if not description.strip():
        missing.append("description")
    else:
        if not _has_any(_REPRO_MARKERS, description):
            missing.append("reproduction steps")
        if not _has_any(_EXPECTED_MARKERS, description):
            missing.append("expected result")
        if not _has_any(_ACTUAL_MARKERS, description):
            missing.append("actual result")

    signals = extract_signals(description, known_repos=known_repos)
    signals["has_comments"] = bool(ticket.comments)
    signals["attachment_count"] = len(ticket.attachments)
    signals["priority"] = str(ticket.priority)
    signals["status"] = str(ticket.status)

    repos = tuple(r for r in known_repos if r.slug in signals["mentioned_repos"])

    reason = (
        f"ticket is missing: {', '.join(missing)}" if missing else "ticket is complete"
    )

    return PreprocessResult(
        ticket=ticket,
        missing=tuple(missing),
        signals=signals,
        repos=repos,
        reason=reason,
    )
