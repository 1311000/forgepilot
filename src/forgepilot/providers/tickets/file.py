"""Tickets read from files — service ``tickets``.

``MockTicketSourcePlugin``'s fixtures are *code*: a test registers them by calling
``add_ticket``. That is right for a test and useless for anything that has to run
a real ticket without a live tracker — an offline demo, a batch run, a replay of
a ticket that has since been closed upstream.

This reads them from disk instead. Every ``*.yaml``/``*.yml``/``*.json`` under
``root`` is a ticket. Configuration, not code, so the ticket a demo runs is the
same file a reader can open and check — and swapping in TAPD later is a row
override rather than a rewrite of whatever hard-coded the ticket.

**Loaded once, at boot, eagerly.** A malformed ticket file is a configuration
error, and the same reasoning that makes ``FORGEPILOT_DEV_TOKENS`` raise on a
malformed table applies: "the ticket is missing" is much harder to diagnose than
"line 4 of T-1.yaml is not valid YAML". Lazy loading would surface the problem
only when that one ticket was fetched, which in a batch run is an hour in.

**Read-only, and it says so.** ``capabilities()`` reports ``read`` and nothing
else, because there is no tracker behind this to record a comment or a
transition. The other methods raise rather than quietly succeeding — a comment
that vanishes is worse than one refused.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from forgepilot.config.errors import ConfigError
from forgepilot.kernel import PluginService
from forgepilot.seams.refs import PrincipalId, TicketId
from forgepilot.seams.tickets import (
    SERVICE_NAME,
    Comment,
    CommentRef,
    Ticket,
    TicketPriority,
    TicketRef,
    TicketStatus,
)

__all__ = ["FileTicketSourcePlugin", "ReadOnlyTicketSource"]

_SUFFIXES = frozenset({".yaml", ".yml", ".json"})


class ReadOnlyTicketSource(RuntimeError):
    """The operation needs a tracker, and this provider is files on disk."""


class FileTicketSourcePlugin(PluginService):
    """Serves ``tickets`` from a directory of ticket files.

    Config::

        root: examples/stat-fix/tickets      # a directory, or one file

    A path is resolved against the current directory. It is deliberately **not**
    resolved against the profile's directory: ``${VAR}`` expansion already exists
    for config values, so a deployment that needs an absolute path can have one,
    and silently reinterpreting a relative path against a different base is how a
    configuration ends up reading a directory nobody named.
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        raw_root = cfg.get("root")
        if not raw_root:
            raise ConfigError(
                "tickets/dir requires `root`: a directory of ticket files. Defaulting "
                "to the current directory would make `--task T-1` work or fail "
                "depending on where it was run from."
            )
        self._root = Path(str(raw_root)).expanduser()
        self._tickets: dict[TicketId, Ticket] = {}
        self._load()

    # -- loading -----------------------------------------------------------
    def _files(self) -> tuple[Path, ...]:
        root = self._root
        if root.is_file():
            return (root,)
        if not root.is_dir():
            raise ConfigError(f"tickets/dir: {root} is neither a file nor a directory")
        return tuple(sorted(p for p in root.rglob("*") if p.suffix.lower() in _SUFFIXES))

    def _read(self, path: Path) -> Any:
        text = path.read_text(encoding="utf-8")
        try:
            if path.suffix.lower() == ".json":
                return json.loads(text)
            return yaml.safe_load(text)
        except (yaml.YAMLError, json.JSONDecodeError) as exc:
            # Named, because a bad ticket in a directory of forty is otherwise
            # something the reader has to find by bisection.
            raise ConfigError(f"{path} is not readable as a ticket: {exc}") from exc

    def _load(self) -> None:
        for path in self._files():
            raw = self._read(path)
            if not isinstance(raw, Mapping):
                raise ConfigError(
                    f"{path} must contain a mapping of ticket fields, got {type(raw).__name__}"
                )
            ticket = _ticket_from(raw, fallback_id=path.stem, source=path)
            if ticket.id in self._tickets:
                raise ConfigError(
                    f"{path} defines ticket {str(ticket.id)!r}, which another file "
                    f"already defined. Two files claiming one id is a mistake, not an "
                    f"override."
                )
            self._tickets[ticket.id] = ticket

        if not self._tickets:
            raise ConfigError(
                f"tickets/dir: no ticket files under {self._root}. An empty ticket "
                f"source would make every `--task` report 'no such ticket', which "
                f"reads as a bug in the tracker rather than an empty directory."
            )

    # -- TicketSource ------------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        """``read`` only. There is no tracker here to record anything else."""
        return frozenset({"read"})

    @property
    def ticket_ids(self) -> tuple[TicketId, ...]:
        """Not part of the seam: what this deployment can run, for `--list-tickets`."""
        return tuple(self._tickets)

    def _refuse(self, what: str) -> ReadOnlyTicketSource:
        return ReadOnlyTicketSource(
            f"{what} needs a ticket source that owns the ticket; this one reads "
            f"{len(self._tickets)} file(s) under {self._root} and cannot write back. "
            f"Point the `tickets` row at a tracker provider to do this."
        )

    async def fetch(self, ticket_id: TicketId) -> Ticket:
        try:
            return self._tickets[ticket_id]
        except KeyError:
            # The known ids are named: "no such ticket" with forty files on disk
            # leaves the reader to guess whether it was a typo or an empty source.
            known = ", ".join(sorted(str(i) for i in self._tickets))
            raise KeyError(f"no such ticket: {str(ticket_id)!r}. Available: {known}") from None

    async def list_linked(self, ticket_id: TicketId) -> Sequence[TicketRef]:
        return (await self.fetch(ticket_id)).related

    async def comment(self, ticket_id: TicketId, body_md: str) -> CommentRef:
        raise self._refuse(f"commenting on {str(ticket_id)!r}")

    async def transition(self, ticket_id: TicketId, to_status: TicketStatus) -> None:
        raise self._refuse(f"transitioning {str(ticket_id)!r} to {to_status.value!r}")

    async def download_attachment(self, attachment_id: str) -> bytes:
        raise self._refuse(f"downloading attachment {attachment_id!r}")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _ticket_from(raw: Mapping[str, Any], *, fallback_id: str, source: Path) -> Ticket:
    """Build a ``Ticket`` from one file's contents.

    The field names are the seam's, with one concession: ``description`` is
    accepted for ``description_md`` because that is what a person writing a ticket
    by hand types. Accepting both is harmless; accepting both *at once* is not —
    which one won would depend on dict ordering, so it raises instead.
    """
    if "description_md" in raw and "description" in raw:
        raise ConfigError(
            f"{source} sets both `description_md` and `description`. They are the "
            f"same field under two names; keep one."
        )
    description = raw.get("description_md", raw.get("description", ""))

    title = raw.get("title")
    if not title:
        raise ConfigError(f"{source} has no `title`. A ticket without one tells the model nothing.")

    return Ticket(
        id=TicketId(str(raw.get("id", fallback_id))),
        title=str(title),
        description_md=str(description or ""),
        status=_enum(TicketStatus, raw.get("status"), TicketStatus.NEW, "status", source),
        priority=_enum(
            TicketPriority, raw.get("priority"), TicketPriority.MEDIUM, "priority", source
        ),
        reporter=_person(raw.get("reporter")),
        assignee=_person(raw.get("assignee")),
        comments=_comments(raw.get("comments") or (), source),
        related=tuple(
            TicketRef(id=TicketId(str(entry)), title="")
            if isinstance(entry, str)
            else TicketRef(id=TicketId(str(entry["id"])), title=str(entry.get("title", "")))
            for entry in (raw.get("related") or ())
        ),
        custom_fields=dict(raw.get("custom_fields") or {}),
        # The verbatim payload, kept so a local classifier can later be trained on
        # real data without a re-fetch. `raw` here is the file's own contents.
        raw=dict(raw),
    )


def _enum(enum_type: Any, value: Any, default: Any, field: str, source: Path) -> Any:
    if value is None:
        return default
    try:
        return enum_type(str(value))
    except ValueError as exc:
        allowed = ", ".join(sorted(member.value for member in enum_type))
        raise ConfigError(f"{source}: unknown {field} {value!r}. One of: {allowed}") from exc


def _person(value: Any) -> PrincipalId | None:
    return PrincipalId(str(value)) if value else None


def _comments(entries: Sequence[Any], source: Path) -> tuple[Comment, ...]:
    out: list[Comment] = []
    for index, entry in enumerate(entries, start=1):
        if isinstance(entry, Mapping):
            body = entry.get("body_md", entry.get("body", ""))
            author = str(entry.get("author", "unknown"))
            comment_id = str(entry.get("id", f"c{index}"))
        else:
            # A bare string is the shape a hand-written fixture wants; there is no
            # author to invent, so it is named rather than guessed at.
            body, author, comment_id = str(entry), "unknown", f"c{index}"
        out.append(Comment(id=comment_id, author=PrincipalId(author), body_md=str(body)))
    return tuple(out)
