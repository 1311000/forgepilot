"""Issue-tracker seam — service name ``tickets``.

Ships against a mock first. The interface is modelled on the real field shapes so
that swapping in TAPD later is a provider change, not a schema migration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from forgepilot.seams.refs import PrincipalId, TicketId

__all__ = [
    "SERVICE_NAME",
    "TicketStatus",
    "TicketPriority",
    "TicketRef",
    "Comment",
    "CommentRef",
    "AttachmentMeta",
    "AttachmentRef",
    "Ticket",
    "TicketSource",
]

SERVICE_NAME = "tickets"


class TicketStatus(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    REJECTED = "rejected"


class TicketPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


@dataclass(frozen=True, slots=True)
class TicketRef:
    id: TicketId
    title: str


@dataclass(frozen=True, slots=True)
class CommentRef:
    id: str


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    id: str
    filename: str


@dataclass(frozen=True, slots=True)
class Comment:
    id: str
    author: PrincipalId
    body_md: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AttachmentMeta:
    id: str
    filename: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class Ticket:
    """A work item, kept faithful to the source system.

    ``raw`` holds the unmodified upstream payload. It is not used by the runtime,
    but keeping it means the local classifier can later be trained on real data
    without a schema migration or a re-fetch of history — and the doc treats that
    post-training path as a goal, not a hypothetical.
    """

    id: TicketId
    title: str
    description_md: str = ""
    status: TicketStatus = TicketStatus.NEW
    priority: TicketPriority = TicketPriority.MEDIUM
    reporter: PrincipalId | None = None
    assignee: PrincipalId | None = None
    comments: tuple[Comment, ...] = ()
    attachments: tuple[AttachmentMeta, ...] = ()
    related: tuple[TicketRef, ...] = ()
    custom_fields: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class TicketSource(Protocol):
    def capabilities(self) -> frozenset[str]: ...

    async def fetch(self, ticket_id: TicketId) -> Ticket: ...

    async def list_linked(self, ticket_id: TicketId) -> Sequence[TicketRef]: ...

    async def comment(self, ticket_id: TicketId, body_md: str) -> CommentRef: ...

    async def transition(self, ticket_id: TicketId, to_status: TicketStatus) -> None: ...

    async def download_attachment(self, attachment_id: str) -> bytes: ...
