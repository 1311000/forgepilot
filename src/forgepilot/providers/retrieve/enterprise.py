"""An `IndexService` over a search service this project did not write.

The reference service in ``tools/serve_index.py`` speaks *our* wire, which makes it a good
conformance fixture and a poor rehearsal for the real problem: an enterprise already runs a
code-search service, and it will not adopt our JSON. This adapter is the second
implementation the seam was supposed to make possible, and its job is to keep four rules at
the boundary even though the other side has never heard of them:

1. **Permission is re-checked here.** The upstream is asked only for repositories the
   credential may read, and anything it volunteers outside that set is dropped and *counted*
   (``dropped_results``), because an upstream ACL and this deployment's constraints are two
   different authorities and the narrower one wins.
2. **Every answer carries a revision.** A hit with no ``revision`` cannot be re-read: the
   caller would edit whatever that path holds today and call it the version it saw. So a
   result without one is refused, not dropped — dropping turns a broken answer into a
   shorter one, which reads as "nothing there".
3. **"Cannot answer" is never "nothing there".** 5xx, a timeout, a body that is not the
   documented shape — all ``IndexUnavailable``. An empty ``results`` list is the only thing
   that means empty.
4. **Freshness is visible.** ``/catalog`` names the revision each repository is indexed at,
   and ``status()`` hands it to ``lag()``; a service that reports no revision for a
   repository is *stale*, not fresh.

The wire below is one plausible foreign shape, documented so a deployment can see what an
adapter costs: it is a *translation*, and every translation is a place where a missing field
becomes a wrong answer if it is not refused.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from forgepilot.kernel import PluginService
from forgepilot.seams.index_service import (
    IndexImporter,
    IndexImportQuery,
    IndexQuery,
    IndexRefused,
    IndexSlice,
    IndexStatus,
    IndexSymbolQuery,
    IndexUnavailable,
)
from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef
from forgepilot.seams.retriever import (
    CodeHit,
    FileSlice,
    RetrieverCapabilities,
    SearchQuery,
    SymbolHit,
    SymbolKind,
    SymbolQuery,
)
from forgepilot.seams.security import ExecutionConstraints

__all__ = [
    "EnterpriseIndexRetrieverPlugin",
    "EnterpriseIndexService",
]

DEFAULT_TIMEOUT_S = 30.0


def _refuse_failed_shards(body: Any, what: str) -> None:
    """Turn a partial answer into a refusal (the reference service's rule, restated here).

    This module may not import another provider — the three-role seam rule — so the two
    helpers it shares in spirit with `providers/retrieve/remote.py` are restated rather than
    borrowed. That is the cost of the rule, and it is the cheaper side of it.
    """
    shards = body.get("shards") if isinstance(body, dict) else None
    failed = (shards or {}).get("failed") or ()
    if not failed:
        return
    named = ", ".join(
        f"{entry.get('shard', '?')} ({entry.get('reason', 'no reason given')})"
        for entry in failed
        if isinstance(entry, dict)
    )
    raise IndexUnavailable(
        f"{what}: {len(failed)} shard(s) did not answer — {named}. "
        "This is not an empty result: those shards could not be asked."
    )


class _Wire:
    """Headers, timeouts and status mapping, in one place for this adapter.

    `transport` exists for tests: an `httpx.MockTransport` exercises the translation without
    a socket. It is a *copy* of the reference client's class on purpose — a provider may not
    import another provider — and the duplication is the price of that rule.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("an index service needs a base URL")
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout_s = float(timeout_s)
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _request(self, method: str, path: str, payload: Any = None) -> Any:
        url = f"{self._base}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.request(method, url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise IndexUnavailable(
                f"could not reach the index at {self._base}: "
                f"{type(exc).__name__}: {exc}. This is not an empty result — "
                f"the index could not be asked."
            ) from None

        if response.status_code in (401, 403):
            raise IndexRefused(
                f"the index refused this credential ({response.status_code}). "
                f"The repositories it covers are not necessarily the ones you may read."
            )
        if response.status_code == 404:
            raise FileNotFoundError(f"the index has no {path}")
        if response.status_code >= 400:
            raise IndexUnavailable(
                f"the index answered {response.status_code} for {path}: "
                f"{response.text[:300]}. This is not an empty result."
            )
        try:
            body = response.json()
        except ValueError:
            raise IndexUnavailable(
                f"the index answered {path} with something that is not JSON: "
                f"{response.text[:200]}. This is not an empty result."
            ) from None
        return body


#: The vendor's paths, named once so a deployment can override them in a subclass.
SEARCH_PATH = "/_search"
SYMBOLS_PATH = "/_symbols"
SLICE_PATH = "/_slice"
CATALOG_PATH = "/_catalog"
LINKS_PATH = "/_links"


def _repo_ref(value: Any) -> RepoRef:
    slug = str(value or "")
    host, _, rest = slug.partition("/")
    namespace, _, name = rest.partition("/")
    if not (host and namespace and name):
        raise IndexUnavailable(
            f"the enterprise index named {slug!r} as a repository. A result whose "
            "repository cannot be read back as host/namespace/name cannot be verified, "
            "so it is refused rather than guessed."
        )
    return RepoRef(host, namespace, name)


def _required(entry: Any, what: str) -> Any:
    if not isinstance(entry, dict) or entry.get(what) in (None, ""):
        raise IndexUnavailable(
            f"the enterprise index returned a result without {what!r}. An answer that "
            "cannot be tied to a revision or a path is the confidently-wrong answer this "
            "seam exists to prevent; it is refused rather than passed on."
        )
    return entry[what]


class EnterpriseIndexService(_Wire):
    """`IndexService` over a vendor's search API, with the seam's rules re-imposed here."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(base_url, token=token, timeout_s=timeout_s, transport=transport)
        #: Results the upstream volunteered for repositories this credential may not read.
        #: Counted rather than silently dropped: an adapter that hides an over-returning
        #: upstream is an adapter nobody can audit.
        self.dropped_results = 0

    # -- helpers ---------------------------------------------------------------------------
    def _asked(self, repos: Sequence[RepoRef], constraints: ExecutionConstraints) -> list[str]:
        for repo in repos:
            if not constraints.allows_repo(repo):
                raise IndexRefused(
                    f"{repo.slug} is not one of this principal's repositories. Refusing "
                    "rather than searching the rest and returning a shorter list, which "
                    "would read as 'there is nothing there'."
                )
        return [repo.slug for repo in repos]

    def _permitted(self, slug: str, constraints: ExecutionConstraints) -> RepoRef | None:
        repo = _repo_ref(slug)
        if constraints.allows_repo(repo):
            return repo
        self.dropped_results += 1
        return None

    async def _post(self, path: str, payload: Any) -> Any:
        body = await self._request("POST", path, payload)
        if not isinstance(body, dict):
            raise IndexUnavailable(
                f"the enterprise index answered {path} with {type(body).__name__}, not the "
                "documented object. An unreadable answer is not an empty one."
            )
        return body

    # -- IndexService ----------------------------------------------------------------------
    async def search(
        self, query: IndexQuery, constraints: ExecutionConstraints
    ) -> Sequence[CodeHit]:
        payload = {
            "query": query.pattern,
            "repositories": self._asked(query.repos, constraints),
            "revision": query.ref,
            "limit": query.max_hits,
            "literal": query.literal,
        }
        if query.globs:
            payload["globs"] = list(query.globs)
        body = await self._post(SEARCH_PATH, payload)
        _refuse_failed_shards(body, "search")
        hits: list[CodeHit] = []
        for entry in body.get("results") or ():
            repo = self._permitted(str(entry.get("repository", "")), constraints)
            if repo is None:
                continue
            hits.append(
                CodeHit(
                    location=CodeRef(
                        repo=repo,
                        path=str(_required(entry, "file")),
                        ref=str(_required(entry, "revision")),
                    ),
                    line=int(entry.get("line") or 1),
                    column=int(entry.get("column") or 1),
                    text=str(entry.get("snippet", "")),
                    score=float(entry.get("score") or 0.0),
                )
            )
        return hits

    async def symbols(
        self, query: IndexSymbolQuery, constraints: ExecutionConstraints
    ) -> Sequence[SymbolHit]:
        payload = {
            "symbol": query.name,
            "repositories": self._asked(query.repos, constraints),
            "revision": query.ref,
            "limit": query.max_hits,
        }
        if query.kind:
            payload["kind"] = query.kind
        body = await self._post(SYMBOLS_PATH, payload)
        _refuse_failed_shards(body, "symbols")
        found: list[SymbolHit] = []
        for entry in body.get("results") or ():
            repo = self._permitted(str(entry.get("repository", "")), constraints)
            if repo is None:
                continue
            found.append(
                SymbolHit(
                    location=CodeRef(
                        repo=repo,
                        path=str(_required(entry, "file")),
                        ref=str(_required(entry, "revision")),
                    ),
                    name=str(query.name),
                    kind=SymbolKind(str(entry.get("kind") or SymbolKind.FUNCTION)),
                    span=LineSpan(int(entry.get("start") or 1), int(entry.get("end") or 1)),
                    signature=entry.get("signature"),
                )
            )
        return found

    async def open(
        self, location: CodeRef, span: LineSpan, constraints: ExecutionConstraints
    ) -> IndexSlice:
        self._asked((location.repo,), constraints)
        body = await self._post(
            SLICE_PATH,
            {
                "repository": location.repo.slug,
                "file": location.path,
                "revision": location.ref,
                "start": span.start,
                "end": span.end,
            },
        )
        text = body.get("text")
        if not isinstance(text, str):
            raise IndexUnavailable(
                "the enterprise index answered a slice request without text. That is not "
                "an empty file; it is a refusal to answer."
            )
        return IndexSlice(
            location=CodeRef(
                repo=location.repo,
                path=location.path,
                ref=str(body.get("revision") or location.ref),
            ),
            span=span,
            text=text,
            truncated=bool(body.get("truncated", False)),
            indexed_at=str(body.get("indexedAt") or body.get("indexed_at") or ""),
        )

    async def importers(
        self, query: IndexImportQuery, constraints: ExecutionConstraints
    ) -> Sequence[IndexImporter]:
        if not query.repos:
            return ()
        try:
            body = await self._post(
                LINKS_PATH,
                {
                    "module": query.module,
                    "repositories": self._asked(query.repos, constraints),
                    "limit": query.max_hits,
                },
            )
        except IndexUnavailable:
            raise
        except FileNotFoundError:
            raise IndexUnavailable(
                "this enterprise index does not answer import questions (it has no "
                "`/_links`). That is a gap in the service, not an absence of importers."
            ) from None
        found: list[IndexImporter] = []
        for entry in body.get("results") or ():
            repo = self._permitted(str(entry.get("repository", "")), constraints)
            if repo is None:
                continue
            found.append(
                IndexImporter(
                    location=CodeRef(
                        repo=repo,
                        path=str(_required(entry, "file")),
                        ref=str(_required(entry, "revision")),
                    ),
                    target=str(entry.get("module") or query.module),
                )
            )
        return found

    async def status(self, constraints: ExecutionConstraints) -> IndexStatus:
        body = await self._request("GET", CATALOG_PATH, None)
        if not isinstance(body, dict):
            raise IndexUnavailable(
                "the enterprise index answered the catalog with "
                f"{type(body).__name__}, not the documented object."
            )
        slugs = [str(slug) for slug in body.get("repositories") or ()]
        revisions = body.get("revisions") or {}
        permitted = [slug for slug in slugs if self._permitted(slug, constraints) is not None]
        return IndexStatus(
            repositories=tuple(_repo_ref(slug) for slug in permitted),
            files=int(body.get("files") or 0),
            updated_at=str(body.get("updatedAt") or body.get("updated_at") or ""),
            refs=tuple(str(ref) for ref in (revisions.get(slug) for slug in permitted) if ref),
            refs_by_repo={slug: str(revisions[slug]) for slug in permitted if revisions.get(slug)},
        )


class EnterpriseIndexRetrieverPlugin(PluginService):
    """`retriever` over the adapter, so the seam's consumer sees no vendor at all.

    It mirrors the reference adapter's two answers: a repository the *caller named* but may
    not read is an error, while the coverage list is intersected silently (that list is this
    deployment's inventory, not the caller's request).
    """

    provide = "retriever"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        settings = dict(config or {})
        token_env = str(settings.get("token_env") or "")
        token = ""
        if token_env:
            import os

            token = os.environ.get(token_env, "")
            if not token:
                raise ValueError(f"`{token_env}` is not set; the index cannot be reached")
        self._repositories = tuple(
            RepoRef(*(str(part) for part in str(slug).split("/")))
            for slug in settings.get("repos", ())
        )
        for repo in self._repositories:
            if not (repo.host and repo.namespace and repo.name):
                raise ValueError(f"repo {repo.slug!r} is not host/namespace/name")
        self._max_hits = int(settings.get("max_hits") or 200)
        self._timeout_s = float(settings.get("timeout_s") or DEFAULT_TIMEOUT_S)
        self._service = EnterpriseIndexService(
            str(settings["base_url"]),
            token=token,
            timeout_s=self._timeout_s,
        )

    def capabilities(self) -> RetrieverCapabilities:
        return RetrieverCapabilities(
            supports_symbols=True,
            supports_remote=True,
            covers_working_tree=False,
        )

    def repositories(self) -> tuple[RepoRef, ...]:
        return self._repositories

    def _asked(
        self, repos: Sequence[RepoRef], constraints: ExecutionConstraints
    ) -> tuple[RepoRef, ...]:
        asked = tuple(repos)
        for repo in asked:
            if not constraints.allows_repo(repo):
                raise PermissionError(
                    f"repo {repo.slug!r} is not permitted. Refusing rather than searching "
                    "the rest and returning a shorter list."
                )
        return asked

    async def search(self, query: SearchQuery, constraints: ExecutionConstraints) -> list[CodeHit]:
        repos = self._asked(query.repos, constraints)
        hits = await self._service.search(
            IndexQuery(
                pattern=query.pattern,
                repos=repos,
                ref=query.ref,
                globs=query.globs,
                max_hits=min(query.max_hits, self._max_hits),
                literal=query.literal,
            ),
            constraints,
        )
        return [hit for hit in hits if constraints.allows_repo(hit.location.repo)]

    async def symbols(
        self, query: SymbolQuery, constraints: ExecutionConstraints
    ) -> list[SymbolHit]:
        repos = self._asked(query.repos, constraints)
        kinds = [str(kind) for kind in query.kinds]
        hits = await self._service.symbols(
            IndexSymbolQuery(
                name=query.name,
                repos=repos,
                ref=query.ref,
                kind=kinds[0] if kinds else None,
                max_hits=min(query.max_hits, self._max_hits),
            ),
            constraints,
        )
        return [hit for hit in hits if constraints.allows_repo(hit.location.repo)]

    async def open(
        self, location: CodeRef, span: LineSpan, constraints: ExecutionConstraints
    ) -> FileSlice:
        self._asked((location.repo,), constraints)
        served = await self._service.open(location, span, constraints)
        return FileSlice(
            location=served.location,
            span=served.span,
            text=served.text,
            truncated=served.truncated,
        )
