"""Tickets from TAPD — service ``tickets``.

The seam was modelled on TAPD's field shapes from the start (``Ticket.raw`` exists
so the upstream payload survives), so this provider is the row override that design
predicted rather than a schema migration.

**Two ways to answer, one vocabic.** ``replay`` reads a *cassette*: the exact JSON
payloads the API returned, keyed by the request that produced them. ``live`` asks
the API. Both go through the same parsing, so a ticket served from a recording is
the ticket a live run would have seen — and the recording is reviewable, which a
credentialled test is not.

**The cassette is written by the provider, not by a separate tool.** With
``record: <path>`` set, a live run appends every payload it received to that file.
Record once with a token, replay forever after without one; a second process that
only has the file gets the same tickets. That is why there is no recorder script to
drift from the reader: the writer and the reader are the same code path.

**A miss is loud.** A cassette that does not hold the request is *not* "no such
ticket" — the two are indistinguishable at the call site and a plausible wrong
answer is worse than a refusal, so ``TapdCassetteMiss`` names the request and the
keys the file does hold.

**Field mapping is explicit and fails closed.** TAPD's statuses are the seam's
statuses, spelled the same way; its priorities are numbers, which the seam is not,
so ``priority_map`` maps them and an unmapped value raises naming the ticket and
the value rather than defaulting to "medium". A silent default is how every bug
becomes a medium.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

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

__all__ = [
    "TapdTicketSourcePlugin",
    "TapdError",
    "TapdCassetteMiss",
    "TapdRequestFailed",
    "TapdReplayIsReadOnly",
]

#: TAPD's timestamp format, which is neither ISO 8601 nor parseable by ``fromisoformat``.
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: TAPD's bug priorities are numbers. The seam's are words, so something has to
#: decide; this is the ordering TAPD documents (1 is the most pressing) and a
#: deployment whose project renames them passes its own map. The values are the
#: enum members, not strings: a mapping table that hands back ``"high"`` still
#: compares equal to ``TicketPriority.HIGH``, which is exactly how a typed field
#: ends up carrying an untyped value nobody notices.
_DEFAULT_PRIORITIES = {
    "1": TicketPriority.URGENT,
    "2": TicketPriority.HIGH,
    "3": TicketPriority.MEDIUM,
    "4": TicketPriority.LOW,
}

_STATUSES = {member.value: member for member in TicketStatus}
_PRIORITIES = {member.value: member for member in TicketPriority}


class TapdError(RuntimeError):
    """The ticket could not be read from, or written to, TAPD."""


class TapdCassetteMiss(TapdError):
    """The recording does not hold the request that was made."""


class TapdRequestFailed(TapdError):
    """TAPD answered, and the answer was a refusal."""


class TapdReplayIsReadOnly(TapdError):
    """A recording cannot record a comment or a transition."""


class TapdTicketSourcePlugin(PluginService):
    """Serves ``tickets`` from TAPD, live or from a cassette.

    Config::

        workspace_id: 1234567                   # required; TAPD project id
        cassette: .tmp/tapd.cassette.json       # replay from here when set
        record: .tmp/tapd.cassette.json         # ...and write live payloads here
        token_env: FORGEPILOT_TAPD_TOKEN        # where the API token is read from
        auth: token                             # `token` (access_token) or `basic`
        api_base: https://api.tapd.cn
        timeout: 30
        priority_map: {"1": urgent, ...}

    ``mode`` is derived rather than guessed at: a cassette means ``replay``, no
    cassette means ``live``, and an explicit ``mode`` overrides both so a test can
    pin one. It is deliberately *not* "live when a token happens to resolve" —
    behaviour that changes with the environment is the kind of thing that makes a
    demo pass on one machine and fail on the next.

    The token is read from the **environment by name**, never from config: a value
    in a patch file lands in `git status` and in `--dump-config`, and `repo`-scoped
    tracker tokens are exactly the kind of thing that must not.
    """

    provide = SERVICE_NAME

    def __init__(
        self,
        ctx: Any,
        config: Any = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._workspace = str(cfg.get("workspace_id") or "").strip()
        if not self._workspace:
            raise ConfigError(
                "tickets/tapd requires `workspace_id`: a TAPD project id. It decides "
                "which project a ticket id is resolved in, so a default would answer "
                "from a project nobody named."
            )
        self._api_base = str(cfg.get("api_base") or "https://api.tapd.cn").rstrip("/")
        self._token_env = str(cfg.get("token_env") or "FORGEPILOT_TAPD_TOKEN")
        self._auth = str(cfg.get("auth") or "token")
        if self._auth not in {"token", "basic"}:
            raise ConfigError(
                f"tickets/tapd: unknown `auth` {self._auth!r}. One of: basic, token."
            )
        self._timeout = float(cfg.get("timeout") or 30.0)
        self._transport = transport
        self._priorities = _enum_map(
            cfg.get("priority_map"), _DEFAULT_PRIORITIES, _PRIORITIES, "priority_map"
        )
        # TAPD's status names are the seam's, spelled the same way, so the default
        # is the identity. A deployment whose project renamed them passes its own
        # map, and a name neither knows fails by name rather than becoming `new`.
        self._statuses = _enum_map(cfg.get("status_map"), _STATUSES, _STATUSES, "status_map")

        cassette = cfg.get("cassette")
        self._cassette_path = Path(str(cassette)).expanduser() if cassette else None
        record = cfg.get("record")
        self._record_path = Path(str(record)).expanduser() if record else None

        mode = str(cfg.get("mode") or "").strip()
        if not mode:
            mode = "replay" if self._cassette_path else "live"
        if mode not in {"replay", "live"}:
            raise ConfigError(
                f"tickets/tapd: unknown `mode` {mode!r}. One of: live, replay."
            )
        self._mode = mode
        if self._mode == "replay" and self._cassette_path is None:
            raise ConfigError(
                "tickets/tapd: `mode: replay` needs `cassette`: the file it replays. "
                "Without one there is nothing to replay and the provider would refuse "
                "every ticket with a message about the network."
            )

        self._responses: dict[str, Any] = {}
        self._recorded_at = ""
        if self._cassette_path is not None:
            self._load_cassette(self._cassette_path)

    # -- cassette ----------------------------------------------------------
    def _load_cassette(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(
                f"tickets/tapd: the cassette {path} is not there. A recording is a file "
                f"a deployment carries; a missing one must not look like an empty "
                f"tracker."
            ) from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"tickets/tapd: {path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("responses"), Mapping
        ):
            raise ConfigError(
                f"tickets/tapd: {path} must hold a `responses` mapping of "
                f"request-key -> payload."
            )
        for key in payload["responses"]:
            if " " not in str(key) or "?" not in str(key):
                raise ConfigError(
                    f"tickets/tapd: {path} has the key {str(key)!r}, which does not say "
                    f"which request it answers. Keys read `METHOD /path?query`."
                )
        self._responses = {str(k): v for k, v in payload["responses"].items()}
        self._recorded_at = str(payload.get("recorded_at") or "")

    def _write_cassette(self) -> None:
        assert self._record_path is not None  # noqa: S101 - guarded by the caller
        self._record_path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "recorded_at": self._recorded_at,
            "workspace_id": self._workspace,
            "responses": self._responses,
        }
        temporary = self._record_path.with_suffix(self._record_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._record_path)

    @staticmethod
    def _key(method: str, path: str, params: Mapping[str, str]) -> str:
        query = urlencode(sorted((k, v) for k, v in params.items() if v != ""))
        return f"{method.upper()} {path}?{query}"

    # -- transport ---------------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        """A recording can be read and nothing else; a live project can be written."""
        if self._mode == "replay":
            return frozenset({"read"})
        return frozenset({"read", "comment", "transition"})

    @property
    def mode(self) -> str:
        """Not part of the seam: which way this provider will answer."""
        return self._mode

    async def _call(
        self, method: str, path: str, params: Mapping[str, str], *, body: Any = None
    ) -> Any:
        if self._mode == "replay":
            key = self._key(method, path, params)
            try:
                return self._responses[key]
            except KeyError:
                known = ", ".join(sorted(self._responses)) or "none"
                raise TapdCassetteMiss(
                    f"the cassette {self._cassette_path} has no answer for {key!r}. "
                    f"It holds: {known}. A recording missing a request is not the same "
                    f"as TAPD saying 'no such ticket'."
                ) from None

        payload = await self._request(method, path, params, body)
        if self._record_path is not None:
            self._responses[self._key(method, path, params)] = payload
            if not self._recorded_at:
                self._recorded_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self._write_cassette()
        return payload

    async def _request(
        self, method: str, path: str, params: Mapping[str, str], body: Any
    ) -> Any:
        token = self._token()
        query = dict(params)
        auth: Any = None
        if self._auth == "token":
            query["access_token"] = token
        else:
            user, _, secret = token.partition(":")
            if not user or not secret:
                raise ConfigError(
                    "tickets/tapd: `auth: basic` needs the credential to read "
                    "`<api_user>:<api_password>`; the value has no colon."
                )
            auth = (user, secret)

        url = f"{self._api_base}{path}"
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            try:
                if method == "GET":
                    answered = await client.get(url, params=query, auth=auth)
                else:
                    answered = await client.post(url, params=query, data=body, auth=auth)
            except httpx.HTTPError as exc:
                # The URL is named without its query, which carries the token.
                raise TapdRequestFailed(
                    f"TAPD did not answer {method} {path}: {type(exc).__name__}. The "
                    f"request never produced a payload, so there is nothing to serve."
                ) from None

        if answered.status_code >= 400:
            raise TapdRequestFailed(
                f"TAPD answered {answered.status_code} to {method} {path}. The body is "
                f"not repeated here because it can echo credentials."
            )
        try:
            payload = answered.json()
        except ValueError:
            raise TapdRequestFailed(
                f"TAPD answered {method} {path} with something that is not JSON."
            ) from None
        return _checked(payload, where=f"{method} {path}")

    def _token(self) -> str:
        token = os.environ.get(self._token_env, "")
        if not token:
            # Named rather than treated as "anonymous": an empty token against a
            # private project answers 401, and "authentication failed" sends the
            # reader hunting for a permissions problem that is really an unset var.
            raise TapdRequestFailed(
                f"no credential: {self._token_env} is unset. The token is read from the "
                f"environment on purpose — a value in a config file lands in git and in "
                f"`--dump-config`."
            )
        return token

    # -- TicketSource ------------------------------------------------------
    async def fetch(self, ticket_id: TicketId) -> Ticket:
        params = {"workspace_id": self._workspace, "id": str(ticket_id)}
        payload = await self._call("GET", "/bugs", params)
        bugs = _objects(payload, "Bug")
        if not bugs:
            raise KeyError(
                f"TAPD workspace {self._workspace} has no bug {str(ticket_id)!r}. The "
                f"answer was empty, which is an answer: the id does not exist there."
            )
        comments: tuple[Comment, ...] = ()
        comment_payload = await self._call(
            "GET",
            "/comments",
            {
                "workspace_id": self._workspace,
                "entry_type": "bug",
                "entry_id": str(ticket_id),
            },
        )
        comments = tuple(_comment_from(entry) for entry in _objects(comment_payload, "Comment"))
        return _ticket_from(
            bugs[0],
            statuses=self._statuses,
            priorities=self._priorities,
            comments=comments,
        )

    async def list_linked(self, ticket_id: TicketId) -> Sequence[TicketRef]:
        """Empty, and honestly so.

        TAPD keeps bug relationships in a separate association API that this
        provider does not model. Returning ``()`` says "none recorded here"; the
        alternative — a plausible guess from ``custom_fields`` — would put a
        relation in a plan that the tracker never stated.
        """
        return ()

    async def comment(self, ticket_id: TicketId, body_md: str) -> CommentRef:
        self._refuse_replay(f"commenting on {str(ticket_id)!r}")
        payload = await self._call(
            "POST",
            "/comments/add",
            {"workspace_id": self._workspace},
            body={
                "entry_type": "bug",
                "entry_id": str(ticket_id),
                "description": body_md,
                "author": self._author(),
            },
        )
        entries = _objects(payload, "Comment")
        if not entries:
            raise TapdRequestFailed(
                f"TAPD accepted the comment on {str(ticket_id)!r} but returned no id, so "
                f"the comment cannot be pointed at later."
            )
        return CommentRef(id=str(entries[0].get("id", "")))

    async def transition(self, ticket_id: TicketId, to_status: TicketStatus) -> None:
        self._refuse_replay(f"transitioning {str(ticket_id)!r}")
        await self._call(
            "POST",
            "/bugs/update",
            {"workspace_id": self._workspace},
            body={"id": str(ticket_id), "status": to_status.value},
        )

    async def download_attachment(self, attachment_id: str) -> bytes:
        raise TapdError(
            f"tickets/tapd does not serve attachments, so {attachment_id!r} cannot be "
            f"downloaded. TAPD's attachment endpoint is not modelled here; a provider "
            f"that returned b'' would look like an empty file instead of a gap."
        )

    def _author(self) -> str:
        identify = getattr(getattr(self.ctx, "principal", None), "identify", None)
        return str(identify) if identify else ""

    def _refuse_replay(self, what: str) -> None:
        if self._mode != "replay":
            return
        raise TapdReplayIsReadOnly(
            f"{what} needs a live tracker; this provider is replaying "
            f"{self._cassette_path} and a recording cannot record. Point `mode` at live "
            f"with a credential to write back."
        )


# ---------------------------------------------------------------------------
# Payload shapes
# ---------------------------------------------------------------------------
def _checked(payload: Any, *, where: str) -> Any:
    """TAPD wraps every answer in ``{"status": 1, "data": ..., "info": "success"}``.

    ``status`` is a *success flag* here, not the bug status field of the same name
    one level down — the collision is TAPD's, and reading the wrong one turns a
    refusal into an empty result.
    """
    if not isinstance(payload, Mapping):
        raise TapdRequestFailed(f"TAPD answered {where} with {type(payload).__name__}.")
    status = payload.get("status")
    if status not in (1, "1"):
        info = str(payload.get("info") or "no reason given")
        raise TapdRequestFailed(f"TAPD refused {where}: {info}")
    return payload


def _objects(payload: Any, kind: str) -> list[Mapping[str, Any]]:
    """Pull the ``{"Bug": {...}}`` wrappers out of a TAPD ``data`` list.

    TAPD answers a list endpoint with a list of single-key envelopes. A mapping is
    accepted too, because the single-object endpoints answer that way and a reader
    should not have to know which one it is holding.
    """
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if data is None:
        return []
    entries = data if isinstance(data, Sequence) and not isinstance(data, (str, bytes)) else [data]
    out: list[Mapping[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        inner = entry.get(kind)
        if isinstance(inner, Mapping):
            out.append(inner)
        elif kind in entry:
            continue
        else:
            out.append(entry)
    return out


def _ticket_from(
    bug: Mapping[str, Any],
    *,
    statuses: Mapping[str, TicketStatus],
    priorities: Mapping[str, TicketPriority],
    comments: tuple[Comment, ...],
) -> Ticket:
    ticket_id = str(bug.get("id") or "").strip()
    if not ticket_id:
        raise TapdRequestFailed(
            "TAPD answered with a bug that has no `id`, so the ticket it belongs to "
            "cannot be named and nothing downstream can cite it."
        )
    title = str(bug.get("title") or "").strip()
    if not title:
        raise TapdRequestFailed(
            f"TAPD bug {ticket_id} has no `title`. A ticket without one tells the model "
            f"nothing, and a placeholder would hide that upstream."
        )
    status_text = str(bug.get("status") or "").strip().lower()
    priority_text = str(bug.get("priority") or "").strip()
    return Ticket(
        id=TicketId(ticket_id),
        title=title,
        description_md=str(bug.get("description") or ""),
        status=_mapped(statuses, status_text, TicketStatus.NEW, "status", ticket_id),
        priority=_mapped(
            priorities, priority_text, TicketPriority.MEDIUM, "priority", ticket_id
        ),
        reporter=_person(bug.get("reporter")),
        assignee=_person(bug.get("owner")),
        comments=comments,
        custom_fields={
            key: value
            for key, value in bug.items()
            if str(key).startswith("custom_field_")
        },
        raw=dict(bug),
    )


def _comment_from(entry: Mapping[str, Any]) -> Comment:
    return Comment(
        id=str(entry.get("id") or ""),
        author=PrincipalId(str(entry.get("author") or "unknown")),
        body_md=str(entry.get("description") or ""),
        created_at=_timestamp(entry.get("created")),
    )


def _mapped(
    table: Mapping[str, Any],
    value: str,
    default: Any,
    field: str,
    ticket_id: str,
) -> Any:
    if not value:
        return default
    try:
        return table[value]
    except KeyError:
        allowed = ", ".join(sorted(table)) or "none"
        raise TapdRequestFailed(
            f"TAPD bug {ticket_id} has {field} {value!r}, which this provider was not "
            f"told how to read. Known values: {allowed}. Map it in `{field}_map` — "
            f"defaulting would turn every bug into the same {field}."
        ) from None


def _enum_map(
    raw: Any,
    default: Mapping[str, str],
    members: Mapping[str, Any],
    name: str,
) -> dict[str, Any]:
    table = dict(default)
    for key, value in dict(raw or {}).items():
        text = str(value).strip().lower()
        if text not in members:
            allowed = ", ".join(sorted(members))
            raise ConfigError(
                f"tickets/tapd: {name}[{str(key)!r}] is {value!r}, which is not a seam "
                f"value. One of: {allowed}"
            )
        table[str(key)] = members[text]
    return table


def _person(value: Any) -> PrincipalId | None:
    text = str(value or "").strip()
    return PrincipalId(text) if text else None


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, _TIME_FORMAT)
    except ValueError:
        # A timestamp the provider cannot read must not stop a ticket: the body is
        # what the plan uses, and the time is decoration on a comment.
        return None
