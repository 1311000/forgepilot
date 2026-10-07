"""Scripted approval — service ``approval``.

Answers approvals from a queue so tests can drive the ``ask`` path without a
human. The default when the queue is empty is
:attr:`ApprovalOutcome.UNAVAILABLE`, not approval: a scripted answerer that
defaulted to "allow" would quietly turn every un-scripted test into a
full-permission test, which is exactly the failure mode fail-closed exists to
prevent.

There is deliberately no way to script an "always allow" grant. Approval is
one-shot; a remembered grant is a permission, and permissions belong to policy.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.approval import (
    SERVICE_NAME,
    ApprovalOutcome,
    ApprovalPolicy,
    ApprovalRequest,
)

__all__ = ["ScriptedApprovalPlugin"]


class ScriptedApprovalPlugin(PluginService):
    """Serves ``approval`` from a queue of outcomes."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._policy = ApprovalPolicy(str(cfg.get("policy", "ask")))
        self._outcomes: deque[ApprovalOutcome] = deque()
        #: Requests that reached the answerer, for assertions.
        self.requests: list[ApprovalRequest] = []

    def script(self, *outcomes: ApprovalOutcome | str) -> "ScriptedApprovalPlugin":
        for outcome in outcomes:
            self._outcomes.append(
                outcome if isinstance(outcome, ApprovalOutcome) else ApprovalOutcome(outcome)
            )
        return self

    @property
    def remaining(self) -> int:
        return len(self._outcomes)

    def policy(self) -> ApprovalPolicy:
        return self._policy

    async def request(self, request: ApprovalRequest) -> ApprovalOutcome:
        self.requests.append(request)
        if self._policy is ApprovalPolicy.NEVER:
            # `never` is enforced before anything else: an answerer cannot be
            # used to route around a policy that says not to ask.
            return ApprovalOutcome.UNAVAILABLE
        if not self._outcomes:
            return ApprovalOutcome.UNAVAILABLE
        return self._outcomes.popleft()
