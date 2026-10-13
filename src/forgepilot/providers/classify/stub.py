"""A classifier that declines to classify — service ``classifier``.

Ships until the company's own model is ready, and it is **deliberately useless**:
it returns no category and zero confidence, always.

That is not laziness, it is the honest option. A keyword heuristic pretending to
be a classifier would look like it worked, and would blur the thing this seam
exists to demonstrate — that the *model* produces understanding and the *harness*
produces decisions. A no-op makes the fallback path the normal path, so the
fallback path gets exercised from the first day instead of being discovered
broken later.

Its output means "unknown", and unknown means **ask a human**. It never guesses.
"""

from __future__ import annotations

from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.classifier import (
    SERVICE_NAME,
    ClassificationRequest,
    TicketUnderstanding,
)

__all__ = ["NullClassifierPlugin"]


class NullClassifierPlugin(PluginService):
    """Serves ``classifier`` by always declining."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        #: Why it declined, surfaced in the understanding so an operator reading
        #: a stuck ticket can tell "no classifier is configured" apart from
        #: "a classifier ran and was unsure". Those need different fixes.
        self.reason: str = str(
            cfg.get("reason", "no classifier is configured; the local model is not ready")
        )

    def capabilities(self) -> frozenset[str]:
        return frozenset({"declines"})

    async def classify(
        self, request: ClassificationRequest, *, session: Any = None
    ) -> TicketUnderstanding:
        # No model, so nothing to record: the session is accepted and ignored.
        return TicketUnderstanding(
            category="",
            confidence=0.0,
            missing=("classification unavailable",),
            notes=self.reason,
        )
