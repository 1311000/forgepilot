"""The frontend-swap boundary: one ASGI app over the runtime.

The plugin here is deliberately thin. It assembles an app and exposes it; it does
**not** start serving on registration. Auto-serving would mean every test that
boots the profile opens a socket, and a plugin that binds a port as a side effect
of being loaded is a plugin you cannot load twice.

``forgepilot --serve`` runs it. Tests drive it through
``httpx.ASGITransport``, which exercises the real routing and the real
dependencies without a socket, a port, or a race to bind one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService

__all__ = ["HttpApiPlugin", "build_app", "credential_dependency", "SERVICE_NAME"]

SERVICE_NAME = "httpapi"


class HttpApiPlugin(PluginService):
    """Serves ``httpapi``: the assembled app, and the registry behind it.

    Declared in the ``web`` bundle and disabled by default there, so a profile
    can include it and still boot on a machine with no web stack installed.
    """

    provide = SERVICE_NAME

    # Declared, not discovered. This plugin reaches `approval` (to install the
    # process-wide answerer), `principal` (to authenticate), `sessionPersistence`
    # (to open sessions), `agents`/`tools`/`sandbox` (to run turns), and an
    # omission is not a quiet `None` here — it raises `cannot get property "X"
    # without inject` on the *first request*, because everything below the route
    # reads through this context.
    #
    # Declaring them is also what makes the plugin fail closed: registered
    # without an approval service, it stays PENDING rather than serving an API
    # whose approval prompts could not be routed or answered.
    inject = ["principal", "sessionPersistence", "approval", "agents", "tools", "sandbox"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self.host = str(cfg.get("host", "127.0.0.1"))
        self.port = int(cfg.get("port", 8000))
        self.approval_timeout = float(cfg.get("approval_timeout", 300.0))

    # ⚠️ This plugin deliberately has **no `app()` and no `serve()`**, and the
    # reason is worth recording because the obvious design is a trap.
    #
    # `cordis_port` wraps a service's methods when they are reached through the
    # proxy it hands back (`utils.create_shadow_method`), and that wrapper is a
    # *synchronous* `def`. Hand a FastAPI instance out through such a method and
    # uvicorn's ASGI-version probe — `iscoroutinefunction(app.__call__)` — reads
    # the wrapper, concludes the app is ASGI 2, and calls it as `app(scope)`.
    # The failure is `FastAPI.__call__() missing 2 required positional arguments`
    # at request time, on a server that started perfectly.
    #
    # So the app is built by the caller, on a context it obtained as a value
    # rather than through a method call: `build_app(root, ...)`, which is what
    # `forgepilot --serve` and every test do. The row here exists so the profile
    # declares the surface and `--boot`/`--dump-config` report it.

    def app_config(self) -> dict[str, Any]:
        """The binding, for whoever is about to build the app.

        Plain data, so it survives the proxy untouched — which is the point.
        """
        return {
            "host": self.host,
            "port": self.port,
            "approval_timeout": self.approval_timeout,
        }


def credential_dependency(root: Any, policy: Any):
    """A FastAPI dependency that turns the request's credential into authority.

    Reads ``Authorization: Bearer <token>`` and nothing else — no query-string
    token, no cookie. A credential in a URL ends up in access logs, browser
    history and ``Referer`` headers, which is three ways to leak one before
    anyone notices.

    Every failure mode is a 401, including "no credential at all". There is no
    anonymous principal, so there is no fallback branch to get wrong.

    The project policy is **not optional**, and leaving it out is not a way to
    avoid restricting anyone: :func:`~forgepilot.identity.constraints.derive`
    reads a missing policy as the most restrictive one, so omitting it denies
    every operation rather than permitting every operation. Which is the safe
    direction to fail, and is why the mistake shows up as a refused tool call
    rather than as an unbounded agent.
    """
    from fastapi import Header
    from fastapi.exceptions import HTTPException

    from forgepilot.httpapi.deps import resolve_credential

    async def dependency(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="a bearer credential is required")
        token = authorization.split(" ", 1)[1].strip()
        if not token:
            raise HTTPException(status_code=401, detail="a bearer credential is required")
        try:
            # The same key that derives the authority also verifies it in
            # `assemble_session`; passing it here is what makes the signature a check
            # rather than a field.
            from forgepilot.identity.signing import configured_key

            return resolve_credential(root, token, project=policy, key=configured_key())
        except Exception as exc:  # noqa: BLE001 - any failure to authenticate is a 401
            # The reason is deliberately not echoed: distinguishing "no such
            # token" from "expired token" tells an attacker which of their guesses
            # were real credentials.
            raise HTTPException(status_code=401, detail="authentication failed") from exc

    return dependency


def resolve_project(workspace_root: Path | None):
    """The project configuration this app serves, and the policy it implies.

    A project on disk contributes its own policy — repositories, permitted
    operations, writable paths — and that is the intended path. With no project
    configured there is nothing to read a policy from, and the two available
    answers are both bad in a way worth naming:

    - the default :class:`ProjectPolicy`, which permits nothing, so every tool
      call is refused and the app looks broken;
    - an open project, which permits a great deal.

    This picks the second and **says so**, because a permissive default that
    nobody is told about is how a demo configuration reaches production. The
    warning is the same shape as the one ``--allow-uncontained-exec`` prints.
    """
    import sys

    from forgepilot.config.project import find_project, project_from_mapping
    from forgepilot.identity.constraints import ProjectPolicy

    if workspace_root is not None:
        found = find_project(Path(workspace_root))
        if found is not None:
            return found, found.to_policy()

    print(
        "⚠  no project.yaml found for the served workspace: deriving authority from "
        "an OPEN project policy.\n"
        "   Every principal's grant is intersected with 'permit everything', so the "
        "grant is the only limit.\n"
        "   Point the server at a configured workspace before this is exposed to "
        "anyone.",
        file=sys.stderr,
    )
    return project_from_mapping({}, name="default"), ProjectPolicy.open_project()


def has_project(workspace_root: Path | None) -> bool:
    """Whether a project is actually configured, not merely assumed.

    `resolve_project` has to return *something* to keep chat working, so it falls
    back to an open project and warns. Task mode cannot use that fallback: a task
    delivers to repositories, and "permit everything, deliver nowhere" would accept
    a ticket and then fail at planning with nothing on screen saying why.
    """
    from forgepilot.config.project import find_project

    return workspace_root is not None and find_project(Path(workspace_root)) is not None


def readiness(stopping: bool, store: Any) -> tuple[int, dict[str, str]]:
    """The readiness decision, as a function so both branches can be exercised.

    A route that answered this inline would have one branch no test could reach:
    a booted container always has a session store, and the kernel's services
    cannot be shadowed per-test (``cannot set property … in multiple fibers``).
    Keeping the decision pure is what lets the "no store" refusal be tested
    rather than merely written down.
    """
    if stopping:
        return 503, {
            "status": "draining",
            "reason": "the process is shutting down and accepts no new work",
        }
    if store is None:
        return 503, {
            "status": "not ready",
            "reason": "no session store is loaded, so a session cannot be opened",
        }
    return 200, {"status": "ready"}


def _replica_name() -> str:
    """A name for this replica in a lease: host, pid, and a random suffix.

    The pid alone is not enough — two replicas can share one across hosts — and
    the suffix keeps a restarted process on the same port from being mistaken for
    the one that held the lease before it.
    """
    import os
    import socket
    import uuid

    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def build_app(
    root: Any,
    *,
    workspace_root: Path | None = None,
    approval_timeout: float = 300.0,
) -> Any:
    """Assemble the ASGI app around a booted container."""
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, PlainTextResponse

    from forgepilot.httpapi.metrics import CONTENT_TYPE, Metrics
    from forgepilot.httpapi.registry import SessionRegistry
    from forgepilot.httpapi.routes import ApiError, build_router

    project, policy = resolve_project(workspace_root)
    metrics = Metrics()
    # The shared half is optional: a profile without a `coordination` row keeps the
    # single-process behaviour (and `--serve`'s lock), which is what every shipped
    # profile does. With the row, a second replica can answer this one's approval
    # and can see that a session is running here (P16).
    coordinator = getattr(root, "coordination", None)
    registry = SessionRegistry(
        root=root,
        approval_timeout=approval_timeout,
        metrics=metrics,
        coordinator=coordinator,
        holder=_replica_name() if coordinator is not None else "",
    )
    # The approval service holds one answerer for the process. Installing it here
    # — once, at assembly — is what makes an approval raised by *any* session
    # reachable by the request that knows its id.
    registry.install()

    # Gauges read the registry at scrape time, so a number cannot be stale: the
    # sessions dict *is* the live set, and a counter kept beside it would be the
    # second copy that drifts.
    metrics.gauge(
        "forgepilot_sessions_live",
        "Sessions this process holds in memory.",
        lambda: len(registry.sessions),
    )
    metrics.gauge(
        "forgepilot_turns_running",
        "Turns currently running.",
        lambda: sum(1 for live in registry.sessions.values() if live.running),
    )
    metrics.gauge(
        "forgepilot_approvals_pending",
        "Approval questions waiting for a human.",
        lambda: len(registry.approvals),
    )
    # The scrapeable form of `/readyz`, for a load balancer that reads metrics
    # instead of probing, and the series a "no ready replica" alert needs: 1 while
    # new work is accepted, 0 once `registry.stop()` has run. It reads the same
    # flag the endpoint does, so the two cannot disagree about whether this
    # process is draining.
    metrics.gauge(
        "forgepilot_ready",
        "1 while this process accepts new work, 0 while it drains.",
        lambda: 0.0 if registry.stopping.is_set() else 1.0,
    )

    @asynccontextmanager
    async def lifespan(_app: Any):
        # Nothing to do on startup: the container is already booted, and
        # lazily doing it here would make the first request the one that
        # discovers a broken profile.
        yield
        # Order matters. `stop()` refuses new turns and releases approval
        # questions so a turn blocked on a human takes its ordinary refusal
        # path; `close()` then cancels whatever is still running and closes the
        # sessions. Reversed, a cancellation lands inside `ask()` and the log
        # loses the reason.
        await registry.stop()
        await registry.close()

    app = FastAPI(title="ForgePilot", version="0.0.1", lifespan=lifespan)
    app.state.registry = registry
    app.state.metrics = metrics

    @app.middleware("http")
    async def _count(request: Request, call_next: Any):
        try:
            response = await call_next(request)
        except Exception:
            # A failed request is still a request. Counting it here is the only
            # place that sees it: the exception handler runs *inside* FastAPI's
            # own middleware, so a 500 raised below never reaches this function
            # as a response.
            route = getattr(request.scope.get("route"), "path", None) or "unmatched"
            metrics.observe_request(request.method, route, 500)
            raise
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"
        metrics.observe_request(request.method, route, response.status_code)
        return response

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": str(exc)})

    app.include_router(
        build_router(
            root,
            registry,
            workspace_root=workspace_root,
            policy=policy,
            project=project,
            project_configured=has_project(workspace_root),
            metrics=metrics,
        )
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Any:
        """Whether this process should receive *new* work.

        Not the same question as ``/healthz``. A draining server answers health
        checks — it is alive, its log is intact, its last turns are finishing —
        and must answer readiness with a refusal, because a load balancer that
        keeps sending it requests during shutdown is how an SSE stream is opened
        on a process that is about to end. That is also why the stop flag, not a
        timer, decides: the same ``registry.stop()`` that ends the streams raises
        it, so the two cannot disagree.
        """
        status, body = readiness(
            registry.stopping.is_set(), getattr(root, "sessionPersistence", None)
        )
        if status == 200:
            return body
        return JSONResponse(status_code=status, content=body)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus() -> Any:
        """Aggregates only, and deliberately unauthenticated.

        Every number here is a count of things, with no tenant, principal,
        session or ticket in a label — so there is nothing to leak that the
        process's own logs do not already say, and a scrape endpoint that needs a
        credential is one a monitoring system cannot be given safely. The moment
        a label names a tenant that stops being true, and this handler has to
        grow an authorisation check in the same change.
        """
        return PlainTextResponse(metrics.render(), media_type=CONTENT_TYPE)

    # The workbench. Served from the package rather than a directory in the
    # repository, so an installed ForgePilot has a UI — and deliberately **not**
    # behind authentication: it holds no data and reveals nothing, and a login
    # wall in front of a static file only makes the credential harder to enter.
    # Every byte of actual content still goes through an authenticated endpoint.
    from fastapi.responses import FileResponse

    index = Path(__file__).parent / "static" / "index.html"

    @app.get("/", include_in_schema=False)
    async def workbench():
        return FileResponse(index, media_type="text/html; charset=utf-8")

    return app
