"""The TAPD ticket source: the row the design predicted, plus its recording.

Two things are being pinned here, and they are different claims.

The **mapping** is the ordinary one: a TAPD payload becomes the seam's ``Ticket``
with its verbatim body in ``raw``, and every value it cannot read fails by name —
an unmapped priority must not quietly become ``medium``, because that is how a
tracker's ordering disappears from every plan at once.

The **cassette** is the one that matters for this repository's habits. Tests here
have to run without a token and without the network, but a test that stubs the
provider proves nothing about the provider. So the recording is written by the
*provider itself*, in a live run, and replayed by the same class in ``replay``
mode: the recorder and the reader are one code path, and the round trip is
exercised as a round trip. A miss in the recording raises and names what the file
does hold — "no such ticket" and "the recording has no such request" are the same
answer at the call site otherwise, and only one of them is true.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from forgepilot.config.errors import ConfigError
from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.providers.tickets.tapd import (
    TapdCassetteMiss,
    TapdError,
    TapdReplayIsReadOnly,
    TapdRequestFailed,
    TapdTicketSourcePlugin,
)
from forgepilot.seams.refs import PrincipalId, TicketId
from forgepilot.seams.tickets import TicketPriority, TicketStatus

WORKSPACE = "1234567"

BUG = {
    "id": "1010101",
    "title": "moving_average 少返回一个窗口",
    "description": "复现步骤：\n1. 调用 moving_average([1, 2, 3, 4], 2)\n预期结果：3 个窗口",
    "status": "in_progress",
    "priority": "2",
    "reporter": "zhang",
    "owner": "li",
    "custom_field_platform": "ios",
    "created": "2026-10-01 10:00:00",
}

COMMENT = {
    "id": "9001",
    "author": "zhang",
    "description": "补充：窗口为 2 时少一个",
    "created": "2026-10-02 09:30:00",
}


def answered(*entries: dict[str, Any]) -> dict[str, Any]:
    """TAPD's envelope: a success flag, the payload list, a human reason."""
    return {"status": 1, "data": list(entries), "info": "success"}


BUGS = answered({"Bug": BUG})
COMMENTS = answered({"Comment": COMMENT})

BUGS_KEY = f"GET /bugs?id=1010101&workspace_id={WORKSPACE}"
COMMENTS_KEY = f"GET /comments?entry_id=1010101&entry_type=bug&workspace_id={WORKSPACE}"


#: The provider reads the API token from the environment by name, never from
#: config, so that a token cannot reach `git status` or `--dump-config`.
TOKEN_ENV = "FORGEPILOT_TAPD_TOKEN"


@pytest.fixture(autouse=True)
def _a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set for every test: replay ignores it, live needs it, and no test dials out."""
    monkeypatch.setenv(TOKEN_ENV, "tok-live")


def write_cassette(path: Path, *, responses: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(
            {
                "recorded_at": "2026-10-18T00:00:00+08:00",
                "workspace_id": WORKSPACE,
                "responses": responses,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


async def close(fibers: tuple[Any, ...]) -> None:
    """LIFO, like the kernel: the provider is disposed before anything it used."""
    for fiber in reversed(fibers):
        await dispose_fiber(fiber)


async def make_source(*, config: dict[str, Any], transport: httpx.BaseTransport | None = None):
    """Boot the way a profile would; a transport makes the HTTP layer observable."""
    root = create_root()

    if transport is None:
        fiber = await root.plugin(TapdTicketSourcePlugin, config)
    else:

        def with_transport(ctx: Any, cfg: Any) -> None:
            TapdTicketSourcePlugin(ctx, cfg, transport=transport)  # type: ignore[arg-type]

        fiber = await root.plugin(with_transport, config)
    return root, (fiber,)


# ---------------------------------------------------------------------------
# Replay: a recording is a run that can be repeated
# ---------------------------------------------------------------------------
async def test_a_cassette_answers_with_the_ticket_the_api_returned(tmp_path: Path) -> None:
    cassette = write_cassette(
        tmp_path / "tapd.json", responses={BUGS_KEY: BUGS, COMMENTS_KEY: COMMENTS}
    )
    root, fibers = await make_source(config={"workspace_id": WORKSPACE, "cassette": str(cassette)})
    try:
        ticket = await root.tickets.fetch(TicketId("1010101"))

        assert ticket.id == TicketId("1010101")
        assert ticket.title == "moving_average 少返回一个窗口"
        assert "复现步骤" in ticket.description_md
        # TAPD's priority is a number and the seam's is a word: the map decides.
        assert ticket.priority is TicketPriority.HIGH
        assert ticket.status is TicketStatus.IN_PROGRESS
        assert ticket.reporter == PrincipalId("zhang")
        assert ticket.assignee == PrincipalId("li")
        assert ticket.comments[0].body_md == "补充：窗口为 2 时少一个"
        assert ticket.comments[0].created_at is not None
        # The upstream payload survives, which is what makes local training possible later.
        assert ticket.raw["title"] == BUG["title"]
        assert ticket.custom_fields == {"custom_field_platform": "ios"}
    finally:
        await close(fibers)


async def test_a_cassette_tells_you_which_requests_it_holds(tmp_path: Path) -> None:
    cassette = write_cassette(
        tmp_path / "tapd.json", responses={BUGS_KEY: BUGS, COMMENTS_KEY: COMMENTS}
    )
    root, fibers = await make_source(config={"workspace_id": WORKSPACE, "cassette": str(cassette)})
    try:
        with pytest.raises(TapdCassetteMiss) as raised:
            await root.tickets.fetch(TicketId("9999999"))

        message = str(raised.value)
        # The request that missed, and what the file does hold: a miss must be
        # diagnosable without opening the cassette.
        assert "9999999" in message
        assert BUGS_KEY in message
    finally:
        await close(fibers)


async def test_a_recording_can_be_read_and_not_written(tmp_path: Path) -> None:
    cassette = write_cassette(
        tmp_path / "tapd.json", responses={BUGS_KEY: BUGS, COMMENTS_KEY: COMMENTS}
    )
    root, fibers = await make_source(config={"workspace_id": WORKSPACE, "cassette": str(cassette)})
    try:
        source = root.tickets

        assert source.capabilities() == frozenset({"read"})
        with pytest.raises(TapdReplayIsReadOnly):
            await source.comment(TicketId("1010101"), "我看看")
        with pytest.raises(TapdReplayIsReadOnly):
            await source.transition(TicketId("1010101"), TicketStatus.CLOSED)
    finally:
        await close(fibers)


# ---------------------------------------------------------------------------
# Live: the request it makes, and what it refuses to guess
# ---------------------------------------------------------------------------
async def test_a_live_fetch_sends_the_token_and_reads_both_endpoints() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = BUGS if request.url.path == "/bugs" else COMMENTS
        return httpx.Response(200, json=payload)

    root, fibers = await make_source(
        config={"workspace_id": WORKSPACE}, transport=httpx.MockTransport(handler)
    )
    try:
        ticket = await root.tickets.fetch(TicketId("1010101"))

        assert ticket.title == BUG["title"]
        assert [r.url.path for r in seen] == ["/bugs", "/comments"]
        # The token is a query parameter in TAPD's open API, so it must be there...
        assert all(r.url.params.get("access_token") == "tok-live" for r in seen)
        # ...and the workspace is what scopes the id, so an omission would resolve
        # the same id in a different project.
        assert all(r.url.params.get("workspace_id") == WORKSPACE for r in seen)
        assert root.tickets.capabilities() == frozenset({"read", "comment", "transition"})
    finally:
        await close(fibers)


async def test_an_unmapped_priority_is_refused_by_name(
    tmp_path: Path,
) -> None:
    odd = answered({"Bug": {**BUG, "priority": "9"}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=odd if request.url.path == "/bugs" else COMMENTS)

    root, fibers = await make_source(
        config={"workspace_id": WORKSPACE}, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(TapdRequestFailed) as raised:
            await root.tickets.fetch(TicketId("1010101"))

        message = str(raised.value)
        assert "1010101" in message
        assert "'9'" in message
        assert "priority_map" in message
    finally:
        await close(fibers)


async def test_a_refusal_is_not_an_empty_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 0, "data": [], "info": "invalid token"})

    root, fibers = await make_source(
        config={"workspace_id": WORKSPACE}, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(TapdRequestFailed) as raised:
            await root.tickets.fetch(TicketId("1010101"))

        # `status: 0` is TAPD's success flag, not the bug's status — reading the
        # wrong one turns a refusal into "no ticket".
        assert "invalid token" in str(raised.value)
    finally:
        await close(fibers)


async def test_a_refusal_never_repeats_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": 0, "info": "token tok-live is invalid"})

    root, fibers = await make_source(
        config={"workspace_id": WORKSPACE}, transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(TapdRequestFailed) as raised:
            await root.tickets.fetch(TicketId("1010101"))

        assert "401" in str(raised.value)
        assert "tok-live" not in str(raised.value)
    finally:
        await close(fibers)


async def test_basic_auth_splits_the_credential_and_keeps_it_out_of_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    monkeypatch.setenv(TOKEN_ENV, "api_user:api_password")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=BUGS if request.url.path == "/bugs" else COMMENTS)

    root, fibers = await make_source(
        config={"workspace_id": WORKSPACE, "auth": "basic"},
        transport=httpx.MockTransport(handler),
    )
    try:
        await root.tickets.fetch(TicketId("1010101"))

        assert "access_token" not in seen[0].url.params
        assert seen[0].headers["authorization"].startswith("Basic ")
    finally:
        await close(fibers)


# ---------------------------------------------------------------------------
# Recording: the writer and the reader are the same code path
# ---------------------------------------------------------------------------
async def test_a_live_run_can_record_what_it_served_and_replay_it(tmp_path: Path) -> None:
    recorded = tmp_path / "tapd.json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=BUGS if request.url.path == "/bugs" else COMMENTS)

    live_root, live_fibers = await make_source(
        config={"workspace_id": WORKSPACE, "record": str(recorded)},
        transport=httpx.MockTransport(handler),
    )
    try:
        live = await live_root.tickets.fetch(TicketId("1010101"))
    finally:
        await close(live_fibers)

    written = json.loads(recorded.read_text(encoding="utf-8"))
    assert written["workspace_id"] == WORKSPACE
    assert set(written["responses"]) == {BUGS_KEY, COMMENTS_KEY}

    # No transport and no token: the only thing this provider has is the file.
    replay_root, replay_fibers = await make_source(
        config={"workspace_id": WORKSPACE, "cassette": str(recorded)}
    )
    try:
        replayed = await replay_root.tickets.fetch(TicketId("1010101"))
    finally:
        await close(replay_fibers)

    assert replayed == live


# ---------------------------------------------------------------------------
# Configuration: a missing answer must be a refusal, not a default
# ---------------------------------------------------------------------------
async def test_replay_without_a_cassette_is_a_configuration_error() -> None:
    with pytest.raises(ConfigError) as raised:
        await make_source(config={"workspace_id": WORKSPACE, "mode": "replay"})

    assert "cassette" in str(raised.value)


async def test_a_missing_workspace_is_refused() -> None:
    with pytest.raises(ConfigError) as raised:
        await make_source(config={})

    assert "workspace_id" in str(raised.value)


async def test_a_malformed_cassette_names_the_file_and_the_key(tmp_path: Path) -> None:
    broken = write_cassette(tmp_path / "tapd.json", responses={"GET /bugs": BUGS})

    with pytest.raises(ConfigError) as raised:
        await make_source(config={"workspace_id": WORKSPACE, "cassette": str(broken)})

    assert str(broken) in str(raised.value)
    assert "'GET /bugs'" in str(raised.value)


async def test_an_absent_cassette_is_not_an_empty_tracker(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as raised:
        await make_source(
            config={"workspace_id": WORKSPACE, "cassette": str(tmp_path / "nope.json")}
        )

    assert "nope.json" in str(raised.value)


# ---------------------------------------------------------------------------
# The seam's other two methods, refused on purpose
# ---------------------------------------------------------------------------
async def test_attachments_are_a_named_gap(tmp_path: Path) -> None:
    cassette = write_cassette(
        tmp_path / "tapd.json", responses={BUGS_KEY: BUGS, COMMENTS_KEY: COMMENTS}
    )
    root, fibers = await make_source(config={"workspace_id": WORKSPACE, "cassette": str(cassette)})
    try:
        # b"" would look like an empty file; a gap has to say it is a gap.
        with pytest.raises(TapdError) as raised:
            await root.tickets.download_attachment("att-1")

        assert "att-1" in str(raised.value)
        assert await root.tickets.list_linked(TicketId("1010101")) == ()
    finally:
        await close(fibers)
