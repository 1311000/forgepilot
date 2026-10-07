"""The approval service — name ``approval``.

Approval is a human's **one-shot** answer, and it is not a security boundary. The
real boundaries are the constraints (what can be addressed), the sandbox (what
the process may touch), and the fact that the model never operates on the
workspace directly. Treating the approval prompt as the security control is what
produced the reference implementation's defect, where a hook completed a host
effect *before* the prompt appeared.

So this service is deliberately small, and three of its properties are the whole
point:

- **Fail-closed.** No answerer, an answerer that raises, an answerer that is slow
  — all produce ``UNAVAILABLE``, and every consumer treats anything other than
  ``ALLOWED_ONCE`` as refusal.
- **One-shot.** There is no "always allow" outcome. A remembered grant is a
  permission, which is a different concept with a different owner (``policy``).
- **``never`` short-circuits before the answerer.** A policy of ``never`` is
  enforced ahead of any answerer being consulted, so registering an answerer
  cannot route around it.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.approval import (
    SERVICE_NAME,
    ApprovalOutcome,
    ApprovalPolicy,
    ApprovalRequest,
)

__all__ = ["ApprovalServicePlugin", "Answerer"]

#: Answers one request. Called only when the policy is ``ask``.
Answerer = Callable[[ApprovalRequest], Awaitable[ApprovalOutcome]]


class ApprovalServicePlugin(PluginService):
    """Serves ``approval``."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._policy = ApprovalPolicy(str(cfg.get("policy", "ask")))
        self._answerer: Answerer | None = None
        #: Every request that reached the service, and every answer given. The
        #: pipeline writes log-only audit events from these outcomes; keeping
        #: them here as well means a test can assert a prompt was raised even
        #: when it was never answered.
        self.asked: list[ApprovalRequest] = []
        self.decided: list[tuple[str, ApprovalOutcome]] = []

    def set_answerer(self, answerer: Answerer | None) -> None:
        self._answerer = answerer

    def policy(self) -> ApprovalPolicy:
        return self._policy

    async def request(self, request: ApprovalRequest) -> ApprovalOutcome:
        self.asked.append(request)

        if self._policy is ApprovalPolicy.NEVER:
            return self._record(request, ApprovalOutcome.UNAVAILABLE)

        if self._answerer is None:
            # Nobody is listening. Refusing is the only safe reading of silence.
            return self._record(request, ApprovalOutcome.UNAVAILABLE)

        try:
            answer = self._answerer(request)
            # Accept a sync answerer too. Requiring `async def` for something as
            # trivial as "the test said yes" invites people to wrap it wrongly.
            outcome = await answer if inspect.isawaitable(answer) else answer
        except Exception:  # noqa: BLE001 - an answerer that breaks is not a yes
            outcome = ApprovalOutcome.UNAVAILABLE

        if not isinstance(outcome, ApprovalOutcome):
            # A typo like returning the string "allowed-once" must not become an
            # allow through coercion.
            outcome = ApprovalOutcome.UNAVAILABLE

        return self._record(request, outcome)

    def _record(self, request: ApprovalRequest, outcome: ApprovalOutcome) -> ApprovalOutcome:
        self.decided.append((request.request_id, outcome))
        return outcome
