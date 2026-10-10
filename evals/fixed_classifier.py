"""A classifier that returns one configured reading — service ``classifier``.

Lives here rather than in `src/` on purpose. It exists for the eval harness and
for nothing else, and adding production surface that only a test drives is the
thing this project keeps finding and deleting.

Two jobs:

- **Scripted mode.** With the real classifier swapped for the replay model, a
  scripted run has no scripted *classification* either, so it declines, and a
  decline is `AWAITING_INFO` — every offline run stops before it starts. This
  removes that.
- **Holding it fixed.** The classifier is a model call, and therefore a source of
  variance that has nothing to do with the mechanism under test. An arm that
  wants to compare "with `code_search`" against "without" is not helped by the
  category also wobbling between runs.

It **declines by default**: `category: null` means `needs_human`, exactly as the
shipped stub does. A classifier that guessed would be worse than one that says it
does not know, in an eval most of all — a guess would be measured as the system's
behaviour.
"""

from __future__ import annotations

from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.classifier import (
    SERVICE_NAME,
    ClassificationRequest,
    TicketUnderstanding,
)

__all__ = ["FixedClassifierPlugin"]


class FixedClassifierPlugin(PluginService):
    """Serves ``classifier`` from config.

    Config::

        category:    bug          # omit or null to decline
        symptom:     ...
        confidence:  0.95
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._understanding = TicketUnderstanding(
            category=str(cfg.get("category") or ""),
            symptom=str(cfg.get("symptom", "")),
            trigger=str(cfg.get("trigger", "")),
            expected=str(cfg.get("expected", "")),
            actual=str(cfg.get("actual", "")),
            notes=str(cfg.get("notes", "")),
            missing=tuple(str(m) for m in cfg.get("missing") or ()),
            confidence=float(cfg.get("confidence", 0.0)),
        )

    def capabilities(self) -> frozenset[str]:
        return frozenset({"classify"})

    async def classify(self, request: ClassificationRequest) -> TicketUnderstanding:
        return self._understanding
