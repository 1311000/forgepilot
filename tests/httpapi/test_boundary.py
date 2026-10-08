"""The HTTP boundary: identity, ownership, and what a status code means here."""

from __future__ import annotations

from tests.httpapi.conftest import OTHER_TOKEN, TOKEN  # noqa: F401  (documented pair)


async def test_a_request_without_a_credential_is_refused(api) -> None:
    response = await api.client.post("/sessions")

    assert response.status_code == 401


async def test_an_unknown_credential_is_refused_and_not_as_anonymous(api) -> None:
    """There is no default identity, so there is no fallback branch to get wrong."""
    response = await api.client.post("/sessions", headers=api.auth("not-a-real-token"))

    assert response.status_code == 401
    assert "anonymous" not in response.text.lower()


async def test_a_credential_in_the_query_string_is_not_accepted(api) -> None:
    """Accepted query tokens end up in access logs, history and Referer headers."""
    response = await api.client.post(f"/sessions?token={TOKEN}")

    assert response.status_code == 401


async def test_creating_a_session_returns_an_owned_id(api) -> None:
    response = await api.client.post("/sessions", headers=api.auth())

    assert response.status_code == 201
    assert response.json()["sessionId"]


async def test_sessions_are_listed_only_for_their_owner(api) -> None:
    mine = await api.client.post("/sessions", headers=api.auth(TOKEN))
    await api.client.post("/sessions", headers=api.auth(OTHER_TOKEN))

    listed = await api.client.get("/sessions", headers=api.auth(TOKEN))

    ids = [s["sessionId"] for s in listed.json()["sessions"]]
    assert ids == [mine.json()["sessionId"]]


async def test_another_principal_cannot_read_or_post_to_my_session(api, session_id) -> None:
    """404, not 403 — a 403 would confirm that the id exists."""
    other = api.auth(OTHER_TOKEN)

    detail = await api.client.get(f"/sessions/{session_id}", headers=other)
    posted = await api.client.post(
        f"/sessions/{session_id}/messages", json={"text": "hi"}, headers=other
    )

    assert detail.status_code == 404
    assert posted.status_code == 404


async def test_another_principal_cannot_answer_my_approval(api, session_id) -> None:
    """Answering is authorised by owning the question, not by knowing its id."""
    from forgepilot.seams.approval import ApprovalRequest
    from forgepilot.seams.refs import SessionId

    live = api.registry.sessions[SessionId(session_id)]
    request = ApprovalRequest(
        request_id="call-1-approval",
        session_id=live.session_id,
        tool_name="write_file",
        arguments_json="{}",
    )
    pending = ApprovalRequest  # keep the import used for typing clarity
    assert pending is ApprovalRequest

    import asyncio

    task = asyncio.create_task(api.registry.ask(request))
    await asyncio.sleep(0)  # let the answerer register its future

    response = await api.client.post(
        f"/approvals/{request.request_id}",
        json={"decision": "allowed-once"},
        headers=api.auth(OTHER_TOKEN),
    )

    assert response.status_code == 404
    task.cancel()


async def test_an_unknown_decision_word_is_refused(api) -> None:
    response = await api.client.post(
        "/approvals/whatever", json={"decision": "always-allow"}, headers=api.auth()
    )

    assert response.status_code == 400
    assert "always allow" in response.json()["error"]


async def test_healthz_needs_no_credential(api) -> None:
    """A liveness probe that needed a credential could not do its job."""
    response = await api.client.get("/healthz")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# The project policy, and the default nobody should be surprised by
# ---------------------------------------------------------------------------
def test_a_configured_workspace_yields_its_own_policy(tmp_path) -> None:
    from forgepilot.httpapi.app import resolve_project
    from forgepilot.seams.security import Operation

    (tmp_path / "project.yaml").write_text(
        "name: svc-a\n"
        "repo_prefixes: ['git.corp/team/*']\n"
        "writable_paths: ['src']\n"
        "allowed_operations: [read_code, write_code]\n",
        encoding="utf-8",
    )

    project, policy = resolve_project(tmp_path)

    assert project.name == "svc-a"
    assert policy.allowed_operations == frozenset({Operation.READ_CODE, Operation.WRITE_CODE})


def test_an_unconfigured_workspace_says_so_out_loud(tmp_path, capsys) -> None:
    """A permissive default that nobody is told about is how a demo configuration
    reaches production.

    There is no good answer here — the restrictive default permits nothing and
    the app looks broken — so the choice is made visible instead.
    """
    from forgepilot.httpapi.app import resolve_project

    project, policy = resolve_project(tmp_path)

    captured = capsys.readouterr()
    assert "no project.yaml" in captured.err
    assert "OPEN project policy" in captured.err
    assert policy.allowed_operations, "the fallback is permissive, which is why it warns"


# ---------------------------------------------------------------------------
# The workbench page
# ---------------------------------------------------------------------------
async def test_the_workbench_page_is_served(api) -> None:
    """A UI that only exists in the repository is a UI an installation lacks."""
    response = await api.client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "ForgePilot" in response.text


async def test_the_page_does_not_get_a_credential_from_the_query_string(api) -> None:
    """It has to carry one in a header — which is why it does not use EventSource.

    ``EventSource`` cannot set an ``Authorization`` header, and the usual
    workaround is a token in the URL. The page reads the stream with ``fetch``
    instead, and this pins the reason: a query credential is refused.
    """
    page = await api.client.get("/")
    assert "EventSource(" not in page.text, "the page must not use EventSource"
    assert "Authorization" in page.text
    assert "Last-Event-ID" in page.text

    refused = await api.client.get(f"/sessions?token={TOKEN}")
    assert refused.status_code == 401


# ---------------------------------------------------------------------------
# The ASGI contract — and the kernel trap that broke it
# ---------------------------------------------------------------------------
async def test_the_app_is_a_genuine_asgi3_application(api) -> None:
    """uvicorn decides ASGI 2 vs 3 by probing ``iscoroutinefunction(__call__)``.

    Get this wrong and the server starts perfectly, then fails every request
    with ``FastAPI.__call__() missing 2 required positional arguments`` — which
    is exactly what happened, and it was found by running the server by hand
    rather than by any test.
    """
    import inspect

    assert inspect.iscoroutinefunction(api.app.__call__)


def test_the_plugin_exposes_config_but_never_hands_out_the_app() -> None:
    """The trap, pinned so it cannot come back quietly.

    ``cordis_port`` wraps a service's methods when they are reached through the
    proxy it hands back, and that wrapper is a *synchronous* ``def``. An ASGI app
    returned through such a method therefore fails uvicorn's coroutine probe and
    is called as ``app(scope)``.

    So the plugin carries configuration — plain data, which crosses the proxy
    untouched — and the app is built by the caller on a context it holds as a
    value. If someone adds ``app()`` or ``serve()`` back to the plugin, this
    test is the reminder of why that ends badly.
    """
    from forgepilot.httpapi.app import HttpApiPlugin

    assert not hasattr(HttpApiPlugin, "app"), "the app must not be produced by a service method"
    assert not hasattr(HttpApiPlugin, "serve")
    assert hasattr(HttpApiPlugin, "app_config")


async def test_the_plugin_config_declares_what_it_reads(api) -> None:
    """A security-relevant service read must be declared, not discovered.

    The kernel raises ``cannot get property "approval" without inject`` on the
    first request when a dependency is undeclared — which is the good outcome.
    The bad one is the root context, whose undeclared reads return ``None``.
    """
    from forgepilot.httpapi.app import HttpApiPlugin

    declared = set(HttpApiPlugin.inject)
    assert {"principal", "approval", "sessionPersistence", "agents", "tools", "sandbox"} <= declared
