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
rather than the classifier. The one thing it does carry is the **session** a
model-backed classifier must record its call in — see
:attr:`ClassifierGateway.session`.
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
    #: The session this classification is recorded in. It lives on the gateway
    #: rather than in ``ClassificationRequest`` because it is **not** part of what a
    #: classifier is shown: putting it in the value object would make it something a
    #: classifier could read, compare or branch on, and the ticket is the only input
    #: the seam is about. It is carried here so `plan_task` keeps its signature — the
    #: caller that has a session is the one that builds the gateway.
    session: Any = None

    async def classify(
        self, request: ClassificationRequest, *, session: Any = None
    ) -> TicketUnderstanding:
        self.calls += 1
        self.requests.append(request)
        # An explicit argument wins over the one this gateway was built with, so a
        # caller holding a session can supply it late without rebuilding the gateway.
        return await self.classifier.classify(
            request, session=session if session is not None else self.session
        )

    def reset(self) -> None:
        self.calls = 0
        self.requests.clear()

    @classmethod
    def wrap(cls, classifier: Any, session: Any = None) -> ClassifierGateway:
        return cls(classifier=classifier, session=session)
