"""Reaching the classifier, and counting how often it is reached.

:class:`ClassifierGateway` wraps a :class:`~forgepilot.seams.classifier.Classifier`
purely to **count invocations**. That count is what makes the design's second
invariant testable: *not every request goes through the model* is a claim about
how many times the classifier ran, and a claim about a count is only meaningful
if the count exists.

Without the counter, "queries skip the classifier" could only be asserted by
reading the code and believing it — which is how that property would quietly stop
being true.

The gateway also passes through unchanged: it does not cache, retry, or
reinterpret. A wrapper with opinions would make the count describe the wrapper
rather than the classifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forgepilot.seams.classifier import (
    ClassificationRequest,
    Classifier,
    TicketUnderstanding,
)

__all__ = ["ClassifierGateway"]


@dataclass
class ClassifierGateway:
    classifier: Classifier
    #: How many times a classification was actually requested.
    calls: int = 0
    #: Every request, kept so a test can assert *what* was classified and not
    #: merely how often.
    requests: list[ClassificationRequest] = field(default_factory=list)

    async def classify(self, request: ClassificationRequest) -> TicketUnderstanding:
        self.calls += 1
        self.requests.append(request)
        return await self.classifier.classify(request)

    def reset(self) -> None:
        self.calls = 0
        self.requests.clear()

    @classmethod
    def wrap(cls, classifier: Any) -> ClassifierGateway:
        return cls(classifier=classifier)
