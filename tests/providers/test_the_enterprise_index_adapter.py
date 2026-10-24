"""P23: the seam's second implementation, against a service we did not write.

The wire is foreign on purpose. These tests are about the four rules the adapter has to keep
at that boundary, not about the vendor's JSON: permission re-checked here, every answer tied
to a revision, "cannot answer" never read as "nothing there", and freshness visible.
"""

from __future__ import annotations

import json

import httpx
import pytest

from forgepilot.providers.retrieve.enterprise import (
    LINKS_PATH,
    EnterpriseIndexService,
)
from forgepilot.seams.index_service import (
    IndexImportQuery,
    IndexQuery,
    IndexRefused,
    IndexSymbolQuery,
    IndexUnavailable,
    lag,
)
from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef
from forgepilot.seams.security import ExecutionConstraints

pytestmark = pytest.mark.asyncio

API = RepoRef("git.corp", "team", "api")
WEB = RepoRef("git.corp", "team", "web")


def _constraints(*repos: RepoRef) -> ExecutionConstraints:
    return ExecutionConstraints(
        principal_id="alice",
        tenant_id="default",
        allowed_repos=frozenset(repos),
        permitted_operations=frozenset({"read_code"}),
    )


def _service(handler, **kwargs) -> EnterpriseIndexService:
    return EnterpriseIndexService(
        "https://search.corp.example",
        token="t",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _json(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode())


# -- search -----------------------------------------------------------------------------------


async def test_a_hit_keeps_the_revision_the_index_named() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _json(
            {
                "results": [
                    {
                        "repository": "git.corp/team/api",
                        "file": "paging/limits.py",
                        "revision": "abc123",
                        "line": 7,
                        "column": 3,
                        "snippet": "MAX_PER_PAGE = 250",
                        "score": 0.9,
                    }
                ]
            }
        )

    hits = await _service(handler).search(
        IndexQuery(pattern="MAX_PER_PAGE", repos=(API,), ref="main"), _constraints(API)
    )
    assert [(hit.location.ref, hit.location.path, hit.line) for hit in hits] == [
        ("abc123", "paging/limits.py", 7)
    ]
    # The revision the caller asked for travels to the service; which revision it answers
    # with is what the caller must be told.
    assert seen["body"]["revision"] == "main"
    assert seen["body"]["repositories"] == ["git.corp/team/api"]


async def test_a_result_without_a_revision_is_refused_not_dropped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json(
            {"results": [{"repository": "git.corp/team/api", "file": "a.py", "snippet": "x"}]}
        )

    with pytest.raises(IndexUnavailable) as refused:
        await _service(handler).search(
            IndexQuery(pattern="x", repos=(API,), ref="main"), _constraints(API)
        )
    assert "without 'revision'" in str(refused.value)
    assert "confidently-wrong" in str(refused.value)


async def test_a_repository_outside_the_credential_is_never_asked_for() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("the request should not be sent")

    with pytest.raises(IndexRefused) as refused:
        await _service(handler).search(
            IndexQuery(pattern="x", repos=(WEB,), ref="main"), _constraints(API)
        )
    assert "not one of this principal's repositories" in str(refused.value)


async def test_a_result_the_caller_did_not_ask_for_is_dropped_and_counted() -> None:
    """The upstream volunteers a repository the caller may not read: the narrower authority
    wins, and the drop is counted so an over-returning service can be audited."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json(
            {
                "results": [
                    {
                        "repository": "git.corp/team/api",
                        "file": "a.py",
                        "revision": "r1",
                        "snippet": "n",
                    },
                    {
                        "repository": "git.corp/team/web",
                        "file": "secret.py",
                        "revision": "r1",
                        "snippet": "n",
                    },
                ]
            }
        )

    service = _service(handler)
    hits = await service.search(
        IndexQuery(pattern="n", repos=(API,), ref="main"), _constraints(API)
    )
    assert [hit.location.repo.slug for hit in hits] == ["git.corp/team/api"]
    assert service.dropped_results == 1


async def test_a_failed_shard_is_a_refusal_not_a_shorter_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json(
            {
                "results": [],
                "shards": {"answered": 1, "failed": [{"shard": "s2", "reason": "timeout"}]},
            }
        )

    with pytest.raises(IndexUnavailable) as refused:
        await _service(handler).search(
            IndexQuery(pattern="x", repos=(API,), ref="main"), _constraints(API)
        )
    assert "not an empty result" in str(refused.value)


async def test_an_empty_result_list_is_the_only_thing_that_means_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json({"results": []})

    assert (
        await _service(handler).search(
            IndexQuery(pattern="nothing", repos=(API,), ref="main"), _constraints(API)
        )
        == []
    )


async def test_a_server_error_is_unavailable_rather_than_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"upstream down")

    with pytest.raises(IndexUnavailable) as refused:
        await _service(handler).search(
            IndexQuery(pattern="x", repos=(API,), ref="main"), _constraints(API)
        )
    assert "not an empty result" in str(refused.value)


async def test_a_body_that_is_not_the_documented_shape_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>login</html>")

    with pytest.raises(IndexUnavailable) as refused:
        await _service(handler).search(
            IndexQuery(pattern="x", repos=(API,), ref="main"), _constraints(API)
        )
    # Either refusal is fine; what matters is that an unreadable body is never an empty
    # result. The transport already names a non-JSON body, so this accepts both.
    message = str(refused.value)
    assert "not JSON" in message or "not the documented object" in message


# -- symbols and slices -----------------------------------------------------------------------


async def test_symbols_carry_the_span_and_the_revision() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json(
            {
                "results": [
                    {
                        "repository": "git.corp/team/api",
                        "file": "paging/limits.py",
                        "revision": "r9",
                        "kind": "function",
                        "start": 10,
                        "end": 14,
                        "signature": "def clamp(requested, limit)",
                    }
                ]
            }
        )

    hits = await _service(handler).symbols(
        IndexSymbolQuery(name="clamp", repos=(API,), ref="main"), _constraints(API)
    )
    assert [(hit.name, hit.kind, hit.span.start, hit.span.end) for hit in hits] == [
        ("clamp", "function", 10, 14)
    ]
    assert hits[0].location.ref == "r9"


async def test_a_slice_says_which_revision_it_came_from() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["repository"] == "git.corp/team/api"
        return _json(
            {"text": "line1\nline2\n", "revision": "r42", "indexedAt": "2026-10-24T00:00:00Z"}
        )

    served = await _service(handler).open(
        CodeRef(repo=API, path="paging/limits.py", ref="main"),
        LineSpan(1, 2),
        _constraints(API),
    )
    assert served.text == "line1\nline2\n"
    assert served.location.ref == "r42"
    assert served.indexed_at.startswith("2026-10-24")


async def test_a_slice_without_text_is_a_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json({"revision": "r1"})

    with pytest.raises(IndexUnavailable) as refused:
        await _service(handler).open(
            CodeRef(repo=API, path="a.py", ref="main"), LineSpan(1, 5), _constraints(API)
        )
    assert "not an empty file" in str(refused.value)


# -- catalog / freshness ----------------------------------------------------------------------


async def test_the_catalog_makes_staleness_visible() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return _json(
            {
                "repositories": ["git.corp/team/api", "git.corp/team/web"],
                "revisions": {"git.corp/team/api": "old", "git.corp/team/web": "old"},
                "files": 12,
                "updatedAt": "2026-10-01T00:00:00Z",
            }
        )

    status = await _service(handler).status(_constraints(API))
    assert status.repositories == (API,)
    assert status.refs_by_repo == {"git.corp/team/api": "old"}
    # The caller knows its own HEAD; the seam's pure function says whether the index is
    # behind it. A repository the catalog omits is stale, not fresh.
    assert lag(status, API, "new").behind
    assert not lag(status, API, "old").behind


async def test_a_catalog_without_a_revision_is_stale_not_fresh() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json({"repositories": ["git.corp/team/api"], "revisions": {}, "files": 1})

    status = await _service(handler).status(_constraints(API))
    assert status.refs_by_repo == {}
    assert lag(status, API, "anything").behind


# -- links ------------------------------------------------------------------------------------


async def test_import_edges_are_answered_when_the_service_has_them() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == LINKS_PATH
        return _json(
            {
                "results": [
                    {
                        "repository": "git.corp/team/api",
                        "file": "paging/limits.py",
                        "revision": "r1",
                        "module": "paging.contract",
                    }
                ]
            }
        )

    found = await _service(handler).importers(
        IndexImportQuery(module="paging.contract", repos=(API,), ref="main"), _constraints(API)
    )
    assert [(hit.location.path, hit.target) for hit in found] == [
        ("paging/limits.py", "paging.contract")
    ]


async def test_a_service_without_links_says_so_rather_than_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"no such endpoint")

    with pytest.raises(IndexUnavailable) as refused:
        await _service(handler).importers(
            IndexImportQuery(module="x", repos=(API,), ref="main"), _constraints(API)
        )
    assert "does not answer import questions" in str(refused.value)
