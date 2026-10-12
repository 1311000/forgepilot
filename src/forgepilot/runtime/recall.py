"""Recall: putting earlier work in front of the model, and saying what backs it.

Phase C *wrote* memories; this is the module that spends them. It exists as its own
module rather than as a few lines inside ``run_turn`` because three separate things
have to agree, and each of them is easy to get subtly wrong on its own:

1. **Which repository this turn is about.** A memory is recalled for a repository,
   and the audience check runs off ``scope_ref``. The caller's authority
   (:class:`~forgepilot.seams.security.ExecutionConstraints`) is the only place a
   repository is named — :class:`~forgepilot.seams.security.Principal` has none — so
   when that authority names zero or several repositories the question has no answer
   and :func:`recaller_for` returns ``None``. That is not a limitation to work
   around later: "show me memories from *a* repository" is a cross-repository read
   wearing a friendly face, and the honest form of it is no recall at all.

2. **What the model is told.** A recalled line is not an instruction, it is not
   verified by being recalled, and the code it describes may have moved since. It is
   also not all equally supported: a line a person wrote or a check proved, a line a
   tool merely *saw*, and a line a model guessed are three different claims, and the
   rendering keeps them apart. Lumping them is the failure the trust order in
   :mod:`forgepilot.seams.memory` exists to prevent, so the prompt must not undo it
   in its headings.

3. **That the log can rebuild the prompt.** Reading memories is not enough: the
   recall becomes a :data:`~forgepilot.seams.session.EventKind.MEMORY_RECALLED`
   event, ``SurfaceOp.APPEND``, carrying the query and the exact text. "Model-visible
   means logged" is the invariant of the whole runtime, and a recall is the clearest
   case of it — if the model saw a line that is not an event, "why did it believe
   that?" has no answer in the log.

Nothing here calls a model. Recall is a lookup plus a rendering, so the per-turn cost
is one store query and no tokens beyond the lines themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from forgepilot.runtime.session import Session
from forgepilot.seams.memory import (
    MEMORY_TYPE_MODES,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    ProvenanceKind,
    RecallQuery,
)
from forgepilot.seams.refs import Mode, TurnId
from forgepilot.seams.security import ExecutionConstraints
from forgepilot.seams.session import EventKind, SurfaceOp

__all__ = [
    "DEFAULT_RECALL_LIMIT",
    "RECALL_HEADING",
    "Recaller",
    "recall_for_turn",
    "recaller_for",
    "render_recalled",
]

#: How many memories one turn may carry. A cap rather than a token budget because
#: the interesting failure is not "the prompt got long", it is "a dozen loosely
#: related lines arrived and the actual question got lost among them".
DEFAULT_RECALL_LIMIT = 5

#: The heading, exported because the workbench and the tests both look for it.
RECALL_HEADING = "## Recalled from earlier sessions in this repository"

#: What each kind of backing actually establishes, in the model's terms. The old
#: heading said "(verified)" over every line, which was true of some of them and —
#: for a line a tool happened to print — of none.
_EVIDENCE_LABEL: dict[str, str] = {
    ProvenanceKind.HUMAN_VERIFIED: "a person said so",
    ProvenanceKind.TEST_VERIFIED: "a check passed",
    ProvenanceKind.TOOL_OBSERVED: "a tool saw it",
    ProvenanceKind.MODEL_ASSERTED: "a model proposed it",
}


@dataclass(frozen=True, slots=True)
class Recaller:
    """Everything needed to ask one repository's memory store one question.

    ``memory`` is typed ``Any`` on purpose: this module must not care whether the
    store is the in-memory one or a SQLite file, and reaching for the provider
    package here would break the seam rule the import-graph test enforces.
    """

    memory: Any
    scope_ref: str
    mode: Mode = Mode.CHAT
    scope: MemoryScope = MemoryScope.REPO
    limit: int = DEFAULT_RECALL_LIMIT


def recaller_for(
    constraints: ExecutionConstraints,
    mode: Mode,
    memory: Any,
    *,
    limit: int = DEFAULT_RECALL_LIMIT,
) -> Recaller | None:
    """A recaller for the one repository this caller is working on — or ``None``.

    ``None`` when the caller's authority names no repository (``forgepilot --chat``
    builds exactly such a grant) or names several. Both cases are refusals, not
    empty results: a turn that is about no repository in particular must not be
    handed one repository's memories, and a turn that is about several must not pick
    one of them on the model's behalf.
    """
    if memory is None:
        return None
    repos = sorted(constraints.allowed_repos, key=lambda repo: repo.slug)
    if len(repos) != 1:
        return None
    return Recaller(memory=memory, scope_ref=repos[0].slug, mode=mode, limit=limit)


def _line(record: MemoryRecord) -> str:
    """One memory as one prompt line: when it was learned, what backs it, what it says."""
    learned: datetime = record.created_at
    label = _EVIDENCE_LABEL.get(record.provenance.kind, str(record.provenance.kind))
    text = " ".join(record.candidate.text.split())
    return f"- ({label}, {learned.date().isoformat()}) {text}"


def render_recalled(records: Sequence[MemoryRecord]) -> str:
    """The block a turn carries, or ``""`` when there is nothing to say.

    Three groups, and the third is the one that matters. The plan called for two —
    "verified" and "speculative" — but ``ACTIVE`` covers more than one kind of
    belief: a line a person stated or a check proved, and a line a tool merely
    happened to print. The second is in the log and nobody has confirmed it, and
    calling it verified is how a record of an observation becomes a fact. So the
    headings are: backed, observed, and unconfirmed.
    """
    if not records:
        return ""

    verified = [record for record in records if record.is_verified]
    observed = [
        record for record in records if record.status is MemoryStatus.ACTIVE and not record.is_verified
    ]
    unconfirmed = [record for record in records if record.status is not MemoryStatus.ACTIVE]

    parts: list[str] = [
        RECALL_HEADING,
        "",
        "These lines were learned in earlier work in this repository. They are **not** part of "
        "what you were just asked, and the code may have changed since — verify anything you are "
        "about to rely on. What backs each line is named after it.",
    ]

    if verified:
        parts += ["", "### Backed by evidence", *(_line(record) for record in verified)]
    if observed:
        parts += [
            "",
            "### Observed, but not confirmed",
            "The log supports these; nobody and nothing has confirmed them.",
            *(_line(record) for record in observed),
        ]
    if unconfirmed:
        parts += [
            "",
            "### Unconfirmed — the harness has not checked these",
            "Treat them as hints. Do not skip a check, a test or a verification because of one.",
            *(_line(record) for record in unconfirmed),
        ]

    return "\n".join(parts)


async def recall_for_turn(
    session: Session,
    *,
    recaller: Recaller,
    text: str,
    constraints: ExecutionConstraints,
    turn: TurnId,
    step: int | None = None,
) -> tuple[MemoryRecord, ...]:
    """Recall once for one turn, and log what the model was shown.

    Returns ``()`` and writes **no event** when nothing matched: an event for an
    empty recall would fill the log with the absence of memories and make the two
    answers "there are none" and "this feature is not wired" look identical.
    """
    query = RecallQuery(
        text=text,
        scope=recaller.scope,
        scope_ref=recaller.scope_ref,
        limit=recaller.limit,
    )
    found = await recaller.memory.recall(query, constraints)
    wanted = tuple(record for record in found if recaller.mode in MEMORY_TYPE_MODES.get(record.type, frozenset()))
    if not wanted:
        return ()

    body = render_recalled(wanted)
    await session.append(
        EventKind.MEMORY_RECALLED,
        {
            "query": query.text,
            "ids": [str(record.id) for record in wanted],
            "text": body,
            "scope": str(query.scope),
            "scopeRef": query.scope_ref,
            "count": len(wanted),
        },
        surface=SurfaceOp.APPEND,
        turn=turn,
        step=step,
    )
    return wanted
