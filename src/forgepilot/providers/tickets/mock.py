"""In-memory ticket source — service ``tickets``.

Fixtures cover the shapes that actually break naive ticket parsing: a report with
no reproduction steps, one that is only a screenshot, one spanning repositories,
and one that is a duplicate of something already closed. Modelling those from the
start is cheaper than discovering them against a live tracker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.refs import PrincipalId, TicketId
from forgepilot.seams.tickets import (
    SERVICE_NAME,
    AttachmentMeta,
    Comment,
    CommentRef,
    Ticket,
    TicketPriority,
    TicketRef,
    TicketStatus,
)

__all__ = ["MockTicketSourcePlugin"]


class MockTicketSourcePlugin(PluginService):
    """Serves ``tickets`` from an in-memory fixture set."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._tickets: dict[TicketId, Ticket] = {}
        self._attachments: dict[str, bytes] = {}
        self._next_comment = 1

    # -- fixtures ----------------------------------------------------------
    def add_ticket(self, ticket: Ticket) -> "MockTicketSourcePlugin":
        self._tickets[ticket.id] = ticket
        return self

    def add_ticket_simple(
        self,
        ticket_id: str,
        title: str,
        description_md: str = "",
        *,
        status: TicketStatus = TicketStatus.NEW,
        priority: TicketPriority = TicketPriority.MEDIUM,
        reporter: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> Ticket:
        """Convenience for the common case; returns the ticket it registered."""
        ticket = Ticket(
            id=TicketId(ticket_id),
            title=title,
            description_md=description_md,
            status=status,
            priority=priority,
            reporter=PrincipalId(reporter) if reporter else None,
            # `raw` stands in for the verbatim upstream payload. Keeping it even
            # in fixtures keeps the later post-training path honest.
            raw=raw if raw is not None else {"id": ticket_id, "title": title},
        )
        self.add_ticket(ticket)
        return ticket

    def add_attachment(self, attachment_id: str, content: bytes) -> "MockTicketSourcePlugin":
        self._attachments[attachment_id] = content
        return self

    # -- TicketSource ------------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        return frozenset({"read", "comment", "transition", "attachments"})

    async def fetch(self, ticket_id: TicketId) -> Ticket:
        try:
            return self._tickets[ticket_id]
        except KeyError:
            raise KeyError(f"no such ticket: {ticket_id!r}") from None

    async def list_linked(self, ticket_id: TicketId) -> Sequence[TicketRef]:
        ticket = await self.fetch(ticket_id)
        return ticket.related

    async def comment(self, ticket_id: TicketId, body_md: str) -> CommentRef:
        ticket = await self.fetch(ticket_id)
        comment_id = f"c{self._next_comment}"
        self._next_comment += 1
        updated = replace(
            ticket,
            comments=ticket.comments
            + (Comment(id=comment_id, author=PrincipalId("forgepilot"), body_md=body_md),),
        )
        self._tickets[ticket_id] = updated
        return CommentRef(id=comment_id)

    async def transition(self, ticket_id: TicketId, to_status: TicketStatus) -> None:
        ticket = await self.fetch(ticket_id)
        self._tickets[ticket_id] = replace(ticket, status=to_status)

    async def download_attachment(self, attachment_id: str) -> bytes:
        try:
            return self._attachments[attachment_id]
        except KeyError:
            raise KeyError(f"no such attachment: {attachment_id!r}") from None

    def attachments_of(self, ticket_id: TicketId) -> tuple[AttachmentMeta, ...]:
        """Not part of the seam; a fixture helper for asserting on reads."""
        ticket = self._tickets.get(ticket_id)
        return ticket.attachments if ticket else ()
