"""``web_fetch``: one URL, one hop, and no host nobody named.

A coding agent reads documentation and changelogs, and refusing to let it reach
the network pushes that work into a shell — where nothing is logged, nothing is
allow-listed and no policy can answer. So the tool exists; what it must not do is
become an oracle for the network it runs next to.

Four rules, in the order they are checked, each closing a different hole:

1. **Nothing leaves unless a host was named.** The allow-list is configuration the
   deployment writes, and its default is empty — "off" is a value, not a missing
   feature. A URL whose host is not on the list is refused by naming the list.
2. **The address, not just the name.** ``localhost``, ``*.internal`` and a public
   name that resolves to ``10.0.0.7`` are the same hole. Every resolved address
   must be globally routable, so a DNS record cannot turn a fetch into a probe of
   the host's own services. This is why the check resolves first and connects to
   nothing before it answers.
3. **One request, no redirects.** A redirect is a second URL chosen by the server —
   exactly the party the allow-list exists to disbelieve. The refusal reports the
   ``Location`` so the model can ask for it directly, where it is checked again.
4. **Text, bounded.** A binary body is refused rather than decoded, and the body is
   truncated at ``max_bytes`` with the truncation said out loud: a silently clipped
   document is a wrong answer that looks complete.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

from forgepilot.kernel.errors import CapabilityNotGranted
from forgepilot.seams.security import Operation
from forgepilot.seams.tools import (
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolOutcome,
)

SERVICE_NAME = "webFetch"

DEFAULT_MAX_BYTES = 200_000
DEFAULT_TIMEOUT_S = 15.0
MAX_URL_LENGTH = 2048

#: Content types that are text by definition. Anything else is refused: decoding a
#: binary body produces replacement characters that read like corruption.
TEXTUAL_PREFIXES = ("text/",)
TEXTUAL_SUFFIXES = ("+json", "+xml", "/json", "/xml", "/x-yaml", "/yaml")

USER_AGENT = "ForgePilot/0.0.1 (+web_fetch)"


def host_allowed(host: str, allow: tuple[str, ...]) -> bool:
    """Exact host, or a ``*.`` suffix whose label boundary is respected.

    ``*.example.com`` covers ``docs.example.com`` and not ``notexample.com``; a
    suffix match that forgets the dot is how an allow-list becomes a suggestion.
    """
    wanted = host.lower().rstrip(".")
    for entry in allow:
        candidate = entry.strip().lower().rstrip(".")
        if not candidate:
            continue
        if candidate.startswith("*."):
            suffix = candidate[1:]  # ".example.com"
            if wanted.endswith(suffix) and len(wanted) > len(suffix):
                return True
        elif wanted == candidate:
            return True
    return False


def _refusal(message: str, **meta: Any) -> ToolOutcome:
    return ToolOutcome(
        content=f"refused: {message}", is_error=True, meta={"phase": "policy", **meta}
    )


def _addresses(host: str, resolver: Any) -> list[str]:
    infos = resolver(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%")[0]).is_global
    except ValueError:
        return False


def build_web_definitions(
    config: dict[str, Any] | None = None,
    *,
    resolver: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[ToolDefinition, ...]:
    """Build ``web_fetch``. ``resolver``/``transport`` exist for tests only."""
    settings = dict(config or {})
    allow = tuple(str(entry) for entry in settings.get("allow", ()) or ())
    max_bytes = int(settings.get("max_bytes", DEFAULT_MAX_BYTES))
    timeout_s = float(settings.get("timeout_s", DEFAULT_TIMEOUT_S))
    resolve = resolver or socket.getaddrinfo

    async def execute(call: ToolCall, ctx: ToolContext) -> ToolOutcome:
        if not ctx.capabilities.network:
            raise CapabilityNotGranted(
                "web_fetch needs the network capability, and this invocation was not granted it"
            )
        if Operation.FETCH_URL not in ctx.constraints.permitted_operations:
            return _refusal(
                "this run may not fetch URLs (the fetch_url operation is not permitted); "
                "ask a human, or use a tool that stays inside the workspace"
            )

        try:
            arguments = json.loads(call.arguments_json or "{}")
        except json.JSONDecodeError as exc:
            return ToolOutcome(
                content=f"the arguments are not JSON: {exc}",
                is_error=True,
                meta={"phase": "arguments"},
            )
        if not isinstance(arguments, dict):
            return ToolOutcome(
                content="the arguments must be an object with a `url`",
                is_error=True,
                meta={"phase": "arguments"},
            )

        url = str(arguments.get("url") or "").strip()
        if not url:
            return ToolOutcome(
                content="web_fetch needs a `url`", is_error=True, meta={"phase": "arguments"}
            )
        if len(url) > MAX_URL_LENGTH:
            return ToolOutcome(
                content=f"the URL is longer than {MAX_URL_LENGTH} characters",
                is_error=True,
                meta={"phase": "arguments"},
            )

        try:
            parts = urlsplit(url)
        except ValueError as exc:
            return ToolOutcome(
                content=f"that is not a URL: {exc}", is_error=True, meta={"phase": "arguments"}
            )

        if parts.scheme != "https":
            return _refusal(f"only https is fetched; {parts.scheme or 'no scheme'!r} is not")
        if parts.username or parts.password:
            return _refusal("a URL that carries credentials is refused: it would land in the log")
        host = (parts.hostname or "").lower()
        if not host:
            return _refusal("the URL names no host")

        if not host_allowed(host, allow):
            if not allow:
                return _refusal(
                    "no host is allowed: this deployment's web_fetch allow-list is empty, which is "
                    "the default. A host has to be named in configuration before it can be read"
                )
            return _refusal(
                f"{host!r} is not in the web_fetch allow-list ({', '.join(sorted(allow))})"
            )

        try:
            addresses = _addresses(host, resolve)
        except OSError as exc:
            return ToolOutcome(
                content=f"cannot resolve {host!r}: {exc}",
                is_error=True,
                meta={"phase": "resolve", "url": url},
            )
        if not addresses:
            return ToolOutcome(
                content=f"{host!r} resolved to no address", is_error=True, meta={"phase": "resolve"}
            )
        private = [address for address in addresses if not _is_public(address)]
        if private:
            return _refusal(
                f"{host!r} resolves to {private[0]!r}, which is not a public address. A name the "
                "deployment can resolve is not the same thing as a host it may reach"
            )

        limit = min(int(arguments.get("max_bytes") or max_bytes), max_bytes)
        headers = {"User-Agent": USER_AGENT, "Accept": "text/*, application/json;q=0.9,*/*;q=0.1"}
        try:
            async with httpx.AsyncClient(
                timeout=timeout_s, follow_redirects=False, transport=transport
            ) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return ToolOutcome(
                content=f"the fetch failed: {type(exc).__name__}: {exc}",
                is_error=True,
                meta={"phase": "transport", "url": url},
            )

        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            return _refusal(
                f"the server answered {response.status_code} and pointed at {location!r}. A redirect "
                "is a second host chosen by the server, so it is not followed: ask for that URL "
                "directly and it will be checked against the allow-list like any other",
                url=url,
                status=response.status_code,
            )

        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not (
            content_type.startswith(TEXTUAL_PREFIXES) or content_type.endswith(TEXTUAL_SUFFIXES)
        ):
            return ToolOutcome(
                content=(
                    f"{url} answered {response.status_code} with {content_type or 'no content type'}, "
                    "which is not text; web_fetch does not decode a binary body"
                ),
                is_error=True,
                meta={"phase": "content-type", "url": url, "status": response.status_code},
            )

        body = response.content[:limit]
        text = body.decode(response.encoding or "utf-8", errors="replace")
        truncated = len(response.content) > limit
        header = f"{url} -> {response.status_code} {content_type}, {len(response.content)} bytes"
        if truncated:
            header += f" (truncated to {limit})"
        return ToolOutcome(
            content=f"{header}\n\n{text}",
            meta={
                "url": url,
                "status": response.status_code,
                "contentType": content_type,
                "bytes": len(response.content),
                "truncated": truncated,
            },
        )

    definition = ToolDefinition(
        name="web_fetch",
        description=(
            "Fetch one https URL from the deployment's allow-list and return its text. "
            "Redirects are not followed and binary bodies are refused; a URL that is not "
            "on the list is refused with the list named."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The https URL to read."},
                "max_bytes": {
                    "type": "integer",
                    "description": f"Upper bound on the body, at most {max_bytes}.",
                },
            },
            "required": ["url"],
        },
        effects=frozenset({ToolEffect.NETWORK}),
        required_operation=Operation.FETCH_URL,
        execute=execute,
    )
    return (definition,)


class WebFetchToolPlugin:
    """Registers ``web_fetch`` when the registry exists.

    Off by default in the strong sense: the plugin is only loaded by
    ``bundles/web_fetch``, and that bundle's allow-list is empty until a deployment
    writes one. Loading the plugin without writing a host therefore registers a
    tool that refuses everything — which is honest, and visible.
    """

    inject = ["tools"]

    def __init__(self, ctx: Any, config: Any = None, **options: Any) -> None:
        self.ctx = ctx
        self.config = dict(config or {})
        self.options = options

    def init(self) -> Any:
        registry = self.ctx.tools
        unregisters = [
            registry.register(definition)
            for definition in build_web_definitions(self.config, **self.options)
        ]

        def cleanup() -> None:
            for unregister in unregisters:
                unregister()

        return cleanup
