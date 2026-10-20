"""The remote index provider: what it sends, what it refuses, and what it drops.

Every case here is about a boundary rather than a happy path, because the happy
path is one request and the boundaries are what a shared corpus makes expensive:
a token that must not learn which repositories exist, a server that returns more
than it was asked for, and a failure that must not read as "nothing there".
"""

from __future__ import annotations

import json

import httpx
import pytest

from forgepilot.providers.retrieve.remote import (
    HttpIndexService,
    RemoteIndexRetrieverPlugin,
)
from forgepilot.seams.index_service import (
    IndexQuery,
    IndexRefused,
    IndexService,
    IndexSymbolQuery,
    IndexUnavailable,
)
from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef
from forgepilot.seams.retriever import SearchQuery, SymbolKind
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation

ALLOWED = RepoRef("git.corp", "team", "svc-a")
OTHER = RepoRef("git.corp", "secret", "payroll")
SLUG = "git.corp/team/svc-a"


def constraints_for(*repos: RepoRef) -> ExecutionConstraints:
    return ExecutionConstraints(
        principal_id="alice",
        tenant_id="acme",
        allowed_repos=frozenset(repos) if repos else frozenset({ALLOWED}),
        permitted_operations=frozenset({Operation.READ_CODE}),
        allowed_commands=None,
        network=NetworkAccess.DENY,
        writable_paths=frozenset(),
    )


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def service(handler, *, token: str = "tok") -> HttpIndexService:
    return HttpIndexService("https://index.corp/api", token=token, transport=transport(handler))


def search_handler(seen: list[httpx.Request], *, extra: list[dict] | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        hits = [
            {
                "repo": SLUG,
                "path": "src/app.py",
                "ref": "main",
                "line": 3,
                "column": 1,
                "text": "MAX_PER_PAGE = 250",
                "score": 1.0,
            }
        ]
        if extra:
            hits.extend(extra)
        return httpx.Response(200, json={"hits": hits, "indexedAt": "2026-10-20T00:00:00Z"})

    return handle


@pytest.mark.asyncio
async def test_a_search_asks_for_the_repos_the_caller_may_read():
    seen: list[httpx.Request] = []
    hits = await service(search_handler(seen)).search(
        IndexQuery(pattern="MAX_PER_PAGE", repos=(ALLOWED,), ref="main"), constraints_for()
    )

    assert [hit.location.path for hit in hits] == ["src/app.py"]
    body = json.loads(seen[0].content)
    assert body["repos"] == [SLUG]
    assert body["pattern"] == "MAX_PER_PAGE"
    assert seen[0].headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_a_hit_from_a_repo_the_caller_may_not_read_is_dropped():
    """A server bug must not become a leak, so the provider re-checks."""
    extra = [
        {
            "repo": "git.corp/secret/payroll",
            "path": "salaries.py",
            "ref": "main",
            "line": 1,
            "column": 1,
            "text": "alice 100",
        }
    ]
    hits = await service(search_handler([], extra=extra)).search(
        IndexQuery(pattern="x", repos=(ALLOWED,), ref="main"), constraints_for()
    )

    assert [hit.location.repo.slug for hit in hits] == [SLUG]


@pytest.mark.asyncio
async def test_a_repo_the_caller_names_and_may_not_read_is_refused_before_the_wire():
    seen: list[httpx.Request] = []
    plugin = plugin_for(
        {"service_url": "https://index.corp/api", "repositories": [SLUG, OTHER.slug]}
    )

    with pytest.raises(PermissionError) as refused:
        await plugin.search(
            SearchQuery(pattern="x", repos=(OTHER,), ref="main"), constraints_for(ALLOWED)
        )

    assert "not permitted" in str(refused.value)
    assert seen == []
    assert plugin.denied_calls == 1


@pytest.mark.asyncio
async def test_open_returns_the_ref_the_service_served_and_its_freshness():
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {
            "repo": SLUG,
            "path": "src/app.py",
            "ref": "<worktree>",
            "start": 1,
            "end": 3,
        }
        return httpx.Response(
            200,
            json={
                "ref": "main",
                "text": "a\nb\n",
                "start": 1,
                "end": 2,
                "truncated": False,
                "indexedAt": "2026-10-19T00:00:00Z",
            },
        )

    slice_ = await service(handle).open(
        CodeRef(repo=ALLOWED, path="src/app.py", ref="<worktree>"),
        LineSpan(1, 3),
        constraints_for(),
    )

    assert slice_.location.ref == "main"
    assert slice_.indexed_at == "2026-10-19T00:00:00Z"
    assert slice_.text == "a\nb\n"


@pytest.mark.asyncio
async def test_symbols_carry_their_kind_and_span():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "repo": SLUG,
                        "path": "src/app.py",
                        "ref": "main",
                        "name": "page",
                        "kind": "function",
                        "start": 4,
                        "end": 6,
                    }
                ],
                "indexedAt": "2026-10-20T00:00:00Z",
            },
        )

    hits = await service(handle).symbols(
        IndexSymbolQuery(name="page", repos=(ALLOWED,), ref="main"),
        constraints_for(),
    )

    assert hits[0].kind is SymbolKind.FUNCTION
    assert hits[0].span == LineSpan(4, 6)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, IndexRefused), (403, IndexRefused), (500, IndexUnavailable), (503, IndexUnavailable)],
)
async def test_a_refusal_and_an_outage_are_different_answers(status, expected):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="no")

    with pytest.raises(expected):
        await service(handle).search(
            IndexQuery(pattern="x", repos=(ALLOWED,), ref="main"), constraints_for()
        )


@pytest.mark.asyncio
async def test_a_missing_file_is_not_an_outage():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not indexed")

    with pytest.raises(FileNotFoundError) as missing:
        await service(handle).open(
            CodeRef(repo=ALLOWED, path="src/gone.py", ref="main"),
            LineSpan(1, 2),
            constraints_for(),
        )

    assert "src/gone.py" in str(missing.value)


@pytest.mark.asyncio
async def test_a_transport_failure_says_it_could_not_ask():
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(IndexUnavailable) as unavailable:
        await service(handle).search(
            IndexQuery(pattern="x", repos=(ALLOWED,), ref="main"), constraints_for()
        )

    assert "not an empty result" in str(unavailable.value)


def test_the_http_service_satisfies_the_seam():
    assert isinstance(service(lambda request: httpx.Response(200, json={})), IndexService)


def test_the_provider_capabilities_do_not_overclaim():
    plugin = plugin_for({"service_url": "https://index.corp/api", "repositories": [SLUG]})
    capabilities = plugin.capabilities()

    assert capabilities.supports_symbols is True
    assert capabilities.supports_remote is True
    # A shared corpus holds committed revisions; claiming the working tree would
    # be the stale-answer defect with a flag on it.
    assert capabilities.covers_working_tree is False


def test_the_provider_needs_a_url_and_a_repository_list():
    with pytest.raises(ValueError, match="service_url"):
        plugin_for({})
    with pytest.raises(ValueError, match="host/namespace/name"):
        plugin_for({"service_url": "https://index.corp/api", "repositories": ["two/parts"]})


@pytest.mark.asyncio
async def test_status_reports_what_the_corpus_covers():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "repositories": 12,
                "files": 4000,
                "updatedAt": "2026-10-20T01:00:00Z",
                "refs": ["main", "release/1.0"],
            },
        )

    status = await service(handle).status(constraints_for())

    assert status.repositories == 12
    assert status.refs == ("main", "release/1.0")


@pytest.mark.asyncio
async def test_the_plugin_opens_through_the_same_seam_as_the_local_provider():
    plugin = plugin_for({"service_url": "https://index.corp/api", "repositories": [SLUG]})
    plugin._transport = transport(
        lambda request: httpx.Response(
            200, json={"ref": "main", "text": "x\n", "start": 1, "end": 1}
        )
    )

    slice_ = await plugin.open(
        CodeRef(repo=ALLOWED, path="src/app.py", ref="<worktree>"),
        LineSpan(1, 1),
        constraints_for(),
    )

    assert slice_.location.ref == "main"
    assert slice_.text == "x\n"
    assert isinstance(plugin.repositories()[0], RepoRef)


def plugin_for(config: dict) -> RemoteIndexRetrieverPlugin:
    """Construct the provider the way the kernel does: `PluginService.__init__`
    registers the service through `ctx.reflect.provide`, so the stand-in has to
    have one. Asserting that it was called with ``"retriever"`` is part of the
    test: a provider that forgot `provide` would be registered under its class
    name and never injected.
    """
    provided: list[str] = []

    class Reflect:
        def provide(self, name, value, check):  # noqa: ANN001, ANN202
            provided.append(name)

    class Context:
        reflect = Reflect()

    built = RemoteIndexRetrieverPlugin(Context(), config)
    assert provided == ["retriever"]
    return built
