"""``web_fetch``: one host that was named, one address that is public, one hop.

The tool is the first one in this repository whose effect leaves the deployment,
so its tests are mostly about the ways that could be abused: an allow-list that
matches by suffix and swallows a lookalike, a public name that resolves inward, a
redirect that chooses a second host, a body that is not text.
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from forgepilot.kernel.errors import CapabilityNotGranted
from forgepilot.runtime.tools.web import build_web_definitions, host_allowed
from forgepilot.seams.security import Operation
from forgepilot.seams.tools import (
    Capabilities,
    ToolCall,
    ToolEffect,
    ToolOutcome,
)
from tests.conftest import constraints_for

URL = "https://docs.example.com/page"

FORGE = Path(__file__).resolve().parents[2]


def _public(host: str, port: int, **_: Any) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def _private(host: str, port: int, **_: Any) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]


def _definition(
    *, allow: tuple[str, ...] = ("docs.example.com",), handler=None, resolver=_public, **config
):
    transport = httpx.MockTransport(handler) if handler is not None else None
    definitions = build_web_definitions(
        {"allow": list(allow), **config}, resolver=resolver, transport=transport
    )
    assert len(definitions) == 1
    return definitions[0]


def _ctx(*, network: bool = True, operations: tuple[Operation, ...] = (Operation.FETCH_URL,)):
    return SimpleNamespace(
        capabilities=Capabilities(network=network),
        constraints=constraints_for(*operations),
    )


def _call(url: str = URL, **extra: Any) -> ToolCall:
    import json

    return ToolCall(
        call_id="c-web", name="web_fetch", arguments_json=json.dumps({"url": url, **extra})
    )


async def _run(definition, call: ToolCall, ctx=None) -> ToolOutcome:
    return await definition.execute(call, ctx or _ctx())


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/html; charset=utf-8"}, text="<html>ok</html>"
    )


# ---------------------------------------------------------------------------
# The allow-list
# ---------------------------------------------------------------------------
def test_a_wildcard_entry_respects_the_label_boundary() -> None:
    allow = ("*.example.com",)

    assert host_allowed("docs.example.com", allow)
    assert host_allowed("a.b.example.com", allow)
    assert not host_allowed("notexample.com", allow)
    assert not host_allowed("example.com", allow)


async def test_the_empty_default_refuses_everything_and_says_why() -> None:
    definition = _definition(allow=())

    outcome = await _run(definition, _call())

    assert outcome.is_error
    assert "allow-list is empty" in outcome.content
    assert "default" in outcome.content


async def test_a_host_not_on_the_list_is_refused_by_naming_the_list() -> None:
    definition = _definition(handler=_ok)

    outcome = await _run(definition, _call("https://evil.example.net/x"))

    assert outcome.is_error
    assert "not in the web_fetch allow-list" in outcome.content
    assert "docs.example.com" in outcome.content


# ---------------------------------------------------------------------------
# The address behind the name
# ---------------------------------------------------------------------------
async def test_a_name_that_resolves_inward_is_refused_before_connecting() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _ok(request)

    definition = _definition(handler=handler, resolver=_private)

    outcome = await _run(definition, _call())

    assert outcome.is_error
    assert "10.0.0.7" in outcome.content
    assert "not a public address" in outcome.content
    assert seen == []  # nothing was contacted


async def test_a_link_local_address_is_refused_too() -> None:
    def resolver(host: str, port: int, **_: Any) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    definition = _definition(handler=_ok, resolver=resolver)

    outcome = await _run(definition, _call())

    assert outcome.is_error
    assert "169.254.169.254" in outcome.content


# ---------------------------------------------------------------------------
# One request, and no second host
# ---------------------------------------------------------------------------
async def test_a_redirect_is_not_followed_and_reports_where_it_pointed() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    definition = _definition(handler=redirect)

    outcome = await _run(definition, _call())

    assert outcome.is_error
    assert "302" in outcome.content
    assert "127.0.0.1/admin" in outcome.content
    assert "not followed" in outcome.content


@pytest.mark.parametrize(
    "url", ["http://docs.example.com/x", "file:///etc/passwd", "ftp://docs.example.com/x"]
)
async def test_only_https_is_fetched(url: str) -> None:
    definition = _definition(handler=_ok)

    outcome = await _run(definition, _call(url))

    assert outcome.is_error
    assert "only https is fetched" in outcome.content


async def test_a_url_carrying_credentials_is_refused() -> None:
    definition = _definition(handler=_ok)

    outcome = await _run(definition, _call("https://user:pass@docs.example.com/x"))

    assert outcome.is_error
    assert "carries credentials" in outcome.content


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------
async def test_a_text_body_is_returned_with_its_status_and_size() -> None:
    definition = _definition(handler=_ok)

    outcome = await _run(definition, _call())

    assert not outcome.is_error
    assert "<html>ok</html>" in outcome.content
    assert "200 text/html" in outcome.content
    assert outcome.meta["status"] == 200
    assert outcome.meta["bytes"] == len("<html>ok</html>")


async def test_a_binary_body_is_refused_rather_than_decoded() -> None:
    def binary(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7")

    definition = _definition(handler=binary)

    outcome = await _run(definition, _call())

    assert outcome.is_error
    assert "not text" in outcome.content


async def test_a_long_body_is_truncated_and_the_header_says_so() -> None:
    def big(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="x" * 500)

    definition = _definition(handler=big, max_bytes=100)

    outcome = await _run(definition, _call())

    assert "truncated to 100" in outcome.content
    assert outcome.meta["truncated"] is True
    # The caller cannot ask for more than the deployment allows.
    capped = await _run(definition, _call(max_bytes=10_000))
    assert capped.meta["truncated"] is True


async def test_a_transport_failure_is_reported_not_raised() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    definition = _definition(handler=boom)

    outcome = await _run(definition, _call())

    assert outcome.is_error
    assert "the fetch failed" in outcome.content
    assert "ConnectError" in outcome.content


# ---------------------------------------------------------------------------
# The two permissions, which are different questions
# ---------------------------------------------------------------------------
async def test_without_the_network_capability_the_call_is_refused_loudly() -> None:
    definition = _definition(handler=_ok)

    with pytest.raises(CapabilityNotGranted):
        await _run(definition, _call(), _ctx(network=False))


async def test_without_the_operation_the_call_is_a_policy_refusal() -> None:
    definition = _definition(handler=_ok)

    outcome = await _run(definition, _call(), _ctx(operations=(Operation.READ_CODE,)))

    assert outcome.is_error
    assert "may not fetch URLs" in outcome.content


def test_the_definition_declares_a_network_effect_and_its_own_operation() -> None:
    definition = _definition()

    assert definition.effects == frozenset({ToolEffect.NETWORK})
    assert definition.required_operation is Operation.FETCH_URL
    assert definition.describe_targets is None


# ---------------------------------------------------------------------------
# Off by default, and visibly so
# ---------------------------------------------------------------------------
def test_the_bundle_is_opt_in_and_ships_no_allowed_host() -> None:
    """Being reachable is not being enabled: the deployment has to write the host.

    Two claims, and both have failed elsewhere in this repository for the same
    reason — a value that looked set by shipping the code. The bundle's allow-list
    must be empty (so loading it grants nothing), and no shipped profile may load
    the bundle (so an operator who does nothing gets no network tool at all).
    """
    import yaml

    patch = yaml.safe_load(
        (FORGE / "src" / "forgepilot" / "bundles" / "web_fetch" / "patch.yaml").read_text(
            encoding="utf-8"
        )
    )
    row = next(entry for entry in patch if entry.get("id") == "toolsWeb")
    assert row["config"]["allow"] == []

    profiles = sorted((FORGE / "configs" / "profiles").glob("*/profile.yaml"))
    assert profiles, "no profile was found, so this test would pass by looking at nothing"
    for profile in profiles:
        raw = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
        assert "web_fetch" not in (raw.get("bundles") or ()), (
            f"{profile.relative_to(FORGE)} loads the web_fetch bundle; nobody chose a host for it"
        )
