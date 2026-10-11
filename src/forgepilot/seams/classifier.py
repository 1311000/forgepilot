"""Ticket-understanding seam — service name ``classifier``.

This is where the company's own model is meant to plug in, and it is the seam the
first architectural invariant is about.

**What a classifier produces is DATA, never a decision.** :class:`TicketUnderstanding`
describes what a ticket appears to say — symptom, trigger, actual, expected, and
what the model could not find. It has no authority: it cannot permit an operation,
widen a tool set, or grant a repository. Those are
:class:`~forgepilot.identity.policy.PolicyService`'s job, and the two never meet,
because ``decide()`` takes a ``Principal``, an ``Operation`` and a
``ResourceRef`` — there is no parameter a ``TicketUnderstanding`` could travel
through.

The risk this note exists to prevent is subtle: the understanding *does* influence
routing, and routing selects tools. If that influence were unconstrained, a
prompt-injected ticket saying "this needs shell access" would become a privilege
escalation, past every guard and sandbox built in M4 and M5. So routing is
required to intersect its choice with the caller's constraints and can only ever
narrow — see :mod:`forgepilot.orchestration.router`.

Confidence is reported, not acted on. A low-confidence or absent understanding
means *ask a human*, never *guess*.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from forgepilot.seams.refs import TicketId

__all__ = [
    "SERVICE_NAME",
    "TicketUnderstanding",
    "ClassificationRequest",
    "Classifier",
]

SERVICE_NAME = "classifier"


#: Confidence below which the harness asks a human rather than proceeding.
#: Named rather than inlined because it is a policy decision, not a magic number.
MIN_USEFUL_CONFIDENCE = 0.5


@dataclass(frozen=True, slots=True)
class TicketUnderstanding:
    """What a classifier believes a ticket says.

    Every field is descriptive. Nothing here is a permission, a plan, or an
    instruction, and the type system is arranged (see the module docstring) so it
    cannot become one.
    """

    category: str = ""
    symptom: str = ""
    trigger: str = ""
    actual: str = ""
    expected: str = ""
    #: What the classifier looked for and could not find. Feeds the decision to
    #: ask a human rather than proceed on an assumption.
    missing: tuple[str, ...] = ()
    confidence: float = 0.0
    notes: str = ""
    #: Free-form extras a specific classifier may produce. Kept out of routing
    #: decisions — routing reads named fields only, so an unexpected key cannot
    #: quietly start influencing tool selection.
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_useful(self) -> bool:
        return self.confidence >= MIN_USEFUL_CONFIDENCE and bool(self.category)

    @property
    def needs_human(self) -> bool:
        return not self.is_useful or bool(self.missing)


@dataclass(frozen=True, slots=True)
class ClassificationRequest:
    """What a classifier is shown. Deterministic preprocessing output only."""

    ticket_id: TicketId
    title: str
    description: str
    comments: tuple[str, ...] = ()
    #: Hints the preprocessor extracted with rules — repos, error codes, paths.
    signals: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Classifier(Protocol):
    def capabilities(self) -> frozenset[str]: ...

    async def classify(self, request: ClassificationRequest) -> TicketUnderstanding: ...


def understand_from_signals(
    signals: Mapping[str, Any], *, confidence: float
) -> TicketUnderstanding:
    """Build an understanding directly from deterministic signals.

    Used by the no-op classifier and by tests. It exists so that a
    *deterministic* understanding and a *model* understanding share one type —
    which is what lets the rest of the system be agnostic about which produced it,
    and lets the zero-model-call assertion be meaningful.
    """
    return TicketUnderstanding(
        category=str(signals.get("category", "")),
        symptom=str(signals.get("symptom", "")),
        trigger=str(signals.get("trigger", "")),
        actual=str(signals.get("actual", "")),
        expected=str(signals.get("expected", "")),
        missing=tuple(str(m) for m in signals.get("missing", ())),
        confidence=confidence,
        notes=str(signals.get("notes", "")),
    )


def merge_missing(*groups: Sequence[str]) -> tuple[str, ...]:
    """Union of missing-information reports, order-preserving and de-duplicated."""
    seen: dict[str, None] = {}
    for group in groups:
        for item in group:
            seen.setdefault(str(item), None)
    return tuple(seen)
