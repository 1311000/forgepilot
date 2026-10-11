"""Approval seam — service name ``approval``.

Approval is a *human's one-shot answer*, and it is not a security boundary. The
real boundaries are the constraints (what can be addressed at all), the sandbox
(what the process may touch), and the fact that the model never operates on the
workspace directly. Treating an approval prompt as a security control is the
mistake that produced the reference implementation's documented defect, where a
pre-execute hook completed a host side effect *before* the prompt appeared and a
later "deny" could do nothing about it.

Two properties are therefore non-negotiable here:

- **Fail-closed.** :attr:`ApprovalOutcome.UNAVAILABLE` is the default when nobody
  answers, and every consumer must treat anything other than
  :attr:`ApprovalOutcome.ALLOWED_ONCE` as a refusal.
- **One-shot.** There is no "always allow" outcome. A remembered grant would be
  a permission, which is a different concept with a different owner.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from forgepilot.seams.refs import SessionId

__all__ = [
    "SERVICE_NAME",
    "ApprovalPolicy",
    "ApprovalOutcome",
    "ApprovalRequest",
    "ApprovalService",
]

SERVICE_NAME = "approval"


class ApprovalPolicy(StrEnum):
    ASK = "ask"
    #: Never ask; unanswerable requests are refused rather than auto-approved.
    NEVER = "never"


class ApprovalOutcome(StrEnum):
    """Closed set. ``ALLOWED_ONCE`` is the only permissive member."""

    ALLOWED_ONCE = "allowed-once"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    #: Nobody answered in time, or no answerer was registered. Fail-closed.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: str
    session_id: SessionId
    tool_name: str
    arguments_json: str
    reason: str = ""
    #: Present so a UI can render the change, not so the answerer can be a model:
    #: an automated answerer is a policy decision and belongs to ``policy``.
    preview: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ApprovalService(Protocol):
    def policy(self) -> ApprovalPolicy: ...

    async def request(self, request: ApprovalRequest) -> ApprovalOutcome: ...
