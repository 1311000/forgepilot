"""What a delivered artifact claims, and which log event says so.

A pull request body is a claim about work that happened elsewhere. Until this module
existed it was assembled from values passed in memory — `checks`, `changed`, `diff` —
so a reviewer had no way to check the body against anything, and a body that drifted
from the log would look exactly like a body that did not. The values were right; what
was missing was the *pointer*.

Every claim here carries the `seq` of the event it came from, so the sentence "the
checks passed" is answered by one line in the log rather than by trust. Two rules:

- **A claim cites an event, not a variable.** If the log does not contain the fact, no
  claim is produced — an empty `## Evidence` section is the honest rendering of a
  delivery that recorded nothing, and it is visible as such.
- **A claim is per repository.** A two-repository run interleaves `task/check` and
  `task/state` events in one log, and attributing repository A's check to repository B's
  pull request is the same class of defect as the cached workspace handle M13 opened with.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

TASK_CHECK = "task/check"
TASK_STATE = "task/state"

__all__ = ["Claim", "claims_for", "render_evidence"]


@dataclass(frozen=True, slots=True)
class Claim:
    """One sentence a reviewer can check, and the event that backs it."""

    text: str
    event: str
    seq: int


def _payload(event: Any) -> Mapping[str, Any]:
    data = getattr(event, "data", None)
    return data if isinstance(data, Mapping) else {}


def _kind(event: Any) -> str:
    return str(getattr(event, "kind", ""))


def _belongs(data: Mapping[str, Any], repo: str) -> bool:
    """Whether an event's `repo` is this delivery's.

    `None` matches: the single-repository callers written before M13 never set the key,
    and their events belong to the only repository the run had.
    """

    named = data.get("repo")
    return named is None or str(named) == repo


def _check_claim(data: Mapping[str, Any], seq: int) -> Claim:
    command = str(data.get("command") or "")
    passed = bool(data.get("passed"))
    if passed:
        text = f"`{command}` — passed"
    else:
        exit_code = data.get("exitCode")
        text = (
            f"`{command}` — FAILED (exit {exit_code})"
            if exit_code is not None
            else f"`{command}` — FAILED"
        )
    meta = data.get("meta")
    if isinstance(meta, Mapping) and meta.get("flaky"):
        attempts = meta.get("attempts", 2)
        text += f" on attempt {attempts} (flaky: the first run failed)"
    return Claim(text, TASK_CHECK, seq)


def claims_for(events: Sequence[Any], *, repo: str) -> tuple[Claim, ...]:
    """Every claim this delivery can make, in log order.

    `verdict` is claimed from the aggregate `task/state` — the one event without a
    `repo` — because the verdict is the run's, not one delivery's. A log written before
    that event existed simply yields no verdict claim.
    """

    claims: list[Claim] = []
    for event in events:
        kind = _kind(event)
        data = _payload(event)
        seq = int(getattr(event, "seq", 0))
        if kind == TASK_CHECK:
            if _belongs(data, repo):
                claims.append(_check_claim(data, seq))
            continue
        if kind != TASK_STATE:
            continue
        if "repo" in data:
            if not _belongs(data, repo):
                continue
            commit = data.get("commit")
            if commit:
                claims.append(Claim(f"commit `{commit}`", TASK_STATE, seq))
            changed = data.get("changed")
            if isinstance(changed, Sequence) and not isinstance(changed, (str, bytes)) and changed:
                claims.append(Claim(f"{len(changed)} path(s) changed", TASK_STATE, seq))
            continue
        state = data.get("state")
        if state:
            claims.append(Claim(f"verdict `{state}`", TASK_STATE, seq))
    return tuple(claims)


def render_evidence(session_id: str, claims: Sequence[Claim]) -> list[str]:
    """The markdown of the `## Evidence` section, or nothing when there are no claims."""

    if not claims:
        return []
    lines = [
        "",
        "## Evidence",
        "",
        f"Session `{session_id}` — every claim above is an event in its log "
        f"(`GET /sessions/{session_id}/events`, or the store's own JSONL).",
    ]
    lines.extend(f"- {claim.text} — event {claim.seq} (`{claim.event}`)" for claim in claims)
    return lines
