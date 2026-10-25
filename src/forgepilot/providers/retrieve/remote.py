"""The shared index, reached over HTTP — the provider a deployment enables.

This is the enterprise end of the retrieval seam: `ForgePilot文档.md` says the
target reads code from a service ("remote-index-first" — 300+ GB across
repositories, a checkout holding metadata only), and `providers/retrieve/grep.py`
is the local overlay. Neither is a substitute for the other, and this module is
the half that was a paragraph until now.

It satisfies **two** interfaces on purpose:

* ``IndexService`` (`seams/index_service.py`) — the protocol a deployment's
  index server must implement, so a team can run their existing search service
  behind a thin adapter instead of adopting this repository's file format.
* ``Retriever`` (`seams/retriever.py`) — the seam tools already talk to, so a
  deployment enables this provider by overriding the ``retriever`` row and no
  tool changes.

## The wire shape (what a deployment must serve)

All bodies are JSON; ``Authorization: Bearer <token>`` when a token is
configured. Every response that carries code also carries ``ref`` and
``indexedAt``, because a slice that does not say which revision it came from and
how old the index is is the confidently-wrong answer the seam is written about.

``POST {base}/search``
    ``{"pattern": str, "repos": [slug...], "ref": str, "globs": [str...],
    "maxHits": int, "literal": bool}``
    → ``{"hits": [{"repo", "path", "ref", "line", "column", "text", "score"}],
    "indexedAt": str}``

``POST {base}/file``
    ``{"repo": slug, "path": str, "ref": str, "start": int, "end": int}``
    → ``{"ref": str, "text": str, "start": int, "end": int, "truncated": bool,
    "indexedAt": str}``

``POST {base}/symbols``
    ``{"name": str, "repos": [slug...], "ref": str, "kinds": [str...],
    "maxHits": int}``
    → ``{"symbols": [{"repo", "path", "ref", "name", "kind", "start", "end",
    "signature"}], "indexedAt": str}``

``GET {base}/status``
    → ``{"repositories": int, "files": int, "updatedAt": str, "refs": [str...]}``

## What it refuses, and how

* A repository the caller may not read is **refused before a request is sent**.
  Sending it and filtering the answer would put the question "may this person see
  that repository" on the wire, where a compromised or buggy server answers it.
* A hit the service returns for a repository the caller may not read is
  **dropped** anyway — the provider re-checks, so a server bug is not a leak.
* 401/403 → `IndexRefused`; 404 → `FileNotFoundError` naming ``repo:path``;
  5xx and transport failures → `IndexUnavailable`. None of them is an empty
  result: "I cannot answer" and "nothing there" lead to opposite decisions.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import httpx

from forgepilot.kernel import PluginService
from forgepilot.seams.index_service import (
    IndexImporter,
    IndexImportQuery,
    IndexQuery,
    IndexRefused,
    IndexService,
    IndexSlice,
    IndexStatus,
    IndexSymbolQuery,
    IndexUnavailable,
    IndexUpdate,
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
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TOKEN_ENV",
    "HttpIndexService",
    "HttpIndexWriter",
    "RemoteIndexRetrieverPlugin",
]

#: The *name* of the variable holding the bearer token, never the value: a token
#: in a config file lands in git and in `--dump-config`.
DEFAULT_TOKEN_ENV = "FORGEPILOT_INDEX_TOKEN"
DEFAULT_TIMEOUT_S = 30.0


def _refuse_failed_shards(body: Any, what: str) -> None:
    """Turn a partial answer into a refusal.

    A fan-out that reports a failed shard has answered *some* of the question. Returning
    the shards that did answer would present that partial set as complete, and the caller
    cannot tell an empty result from an unasked shard — the confidently-wrong answer this
    seam's first rule forbids. So the call fails, naming the shards.
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
    """The transport the read and write sides share.

    One class for headers, timeouts and status mapping, so the write side cannot
    drift into its own idea of what a 403 means. ``transport`` exists for tests:
    ``httpx.MockTransport`` exercises the wire shape without a socket.
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

    # -- wire --------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _request(self, method: str, path: str, payload: Any = None) -> Any:
        url = f"{self._base}{path}"
        try:
            # 	rust_env=False is a correctness fix, not a preference: with the default
            # an \HTTP_PROXY\ (or a Windows registry proxy) is applied to *every* URL,
            # including W.0.0.1\, so a dead machine-wide proxy makes a local index look
            # unreachable. Measured on this box: identical code, \ConnectError\ with
            # trust_env on and 200 with it off. A deployment that needs an egress proxy
            # should configure that proxy, not inherit one silently.
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport, trust_env=False
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
            raise FileNotFoundError(
                f"{payload.get('repo', '?') if isinstance(payload, dict) else '?'}:"
                f"{payload.get('path', '?') if isinstance(payload, dict) else '?'} "
                f"is not in the index"
            )
        if response.status_code >= 500:
            raise IndexUnavailable(
                f"the index answered {response.status_code} at {path}: "
                f"{response.text[:200]!r}. This is not an empty result."
            )
        if response.status_code >= 400:
            raise IndexUnavailable(
                f"the index answered {response.status_code} at {path} "
                f"(a request this provider believes it can make): {response.text[:200]!r}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise IndexUnavailable(
                f"the index answered {path} with something that is not JSON: {exc}"
            ) from None


class HttpIndexService(_Wire):
    """`IndexService` over HTTP: search, symbols, a slice, and the status."""

    __slots__ = ()

    # -- IndexService ------------------------------------------------------
    async def search(self, query: IndexQuery, constraints: ExecutionConstraints) -> list[CodeHit]:
        payload = {
            "pattern": query.pattern,
            "repos": [repo.slug for repo in query.repos],
            "ref": query.ref,
            "globs": list(query.globs),
            "maxHits": query.max_hits,
            "literal": query.literal,
        }
        body = await self._request("POST", "/search", payload)
        hits: list[CodeHit] = []
        for entry in body.get("hits", ()):
            location = CodeRef(
                repo=_repo(str(entry["repo"])),
                path=str(entry["path"]),
                ref=str(entry.get("ref", query.ref)),
            )
            hits.append(
                CodeHit(
                    location=location,
                    line=int(entry["line"]),
                    column=int(entry.get("column", 1)),
                    text=str(entry.get("text", "")),
                    score=float(entry.get("score", 0.0)),
                )
            )
        # Re-check here rather than trusting the caller's list: this adapter is
        # the last place between a remote answer and the model. A service that
        # returns a repository the principal may not read is a server bug, and a
        # server bug must not become a leak.
        _refuse_failed_shards(body, "search")
        return [hit for hit in hits if constraints.allows_repo(hit.location.repo)]

    async def symbols(
        self, query: IndexSymbolQuery, constraints: ExecutionConstraints
    ) -> list[SymbolHit]:
        payload = {
            "name": query.name,
            "repos": [repo.slug for repo in query.repos],
            "ref": query.ref,
            "kinds": [] if query.kind is None else [query.kind],
            "maxHits": query.max_hits,
        }
        body = await self._request("POST", "/symbols", payload)
        hits: list[SymbolHit] = []
        for entry in body.get("symbols", ()):
            hits.append(
                SymbolHit(
                    location=CodeRef(
                        repo=_repo(str(entry["repo"])),
                        path=str(entry["path"]),
                        ref=str(entry.get("ref", query.ref)),
                    ),
                    name=str(entry["name"]),
                    kind=SymbolKind(str(entry.get("kind", SymbolKind.FUNCTION))),
                    span=LineSpan(int(entry["start"]), int(entry["end"])),
                    signature=entry.get("signature"),
                )
            )
        _refuse_failed_shards(body, "symbols")
        return [hit for hit in hits if constraints.allows_repo(hit.location.repo)]

    async def importers(
        self, query: IndexImportQuery, constraints: ExecutionConstraints
    ) -> Sequence[IndexImporter]:
        """Who imports this module or package, answered by the service.

        No shard bookkeeping: edges are one lookup on the service side, and the shards that
        exist are of *files*. An empty ``repos`` sends an empty list — the service reads
        that as "no repository", never "all of them".
        """
        if not query.repos:
            return ()
        payload = {
            "module": query.module,
            "repos": [repo.slug for repo in query.repos],
            "maxHits": query.max_hits,
        }
        if query.ref:
            payload["ref"] = query.ref
        body = await self._request("POST", "/importers", payload)
        found: list[IndexImporter] = []
        for entry in body.get("importers", ()):
            repo = _repo(str(entry["repo"]))
            if not constraints.allows_repo(repo):
                continue
            found.append(
                IndexImporter(
                    location=CodeRef(
                        repo=repo,
                        path=str(entry["path"]),
                        ref=str(entry.get("ref", query.ref)),
                    ),
                    target=str(entry["target"]),
                )
            )
        return found

    async def open(
        self, location: CodeRef, span: LineSpan, constraints: ExecutionConstraints
    ) -> IndexSlice:
        payload = {
            "repo": location.repo.slug,
            "path": location.path,
            "ref": location.ref,
            "start": span.start,
            "end": span.end,
        }
        body = await self._request("POST", "/file", payload)
        served = str(body.get("ref", location.ref))
        return IndexSlice(
            location=CodeRef(repo=location.repo, path=location.path, ref=served),
            span=LineSpan(int(body.get("start", span.start)), int(body.get("end", span.end))),
            text=str(body.get("text", "")),
            truncated=bool(body.get("truncated", False)),
            indexed_at=str(body.get("indexedAt", "")),
        )

    async def status(self, constraints: ExecutionConstraints) -> IndexStatus:
        body = await self._request("GET", "/status")
        return IndexStatus(
            repositories=int(body.get("repositories", 0)),
            files=int(body.get("files", 0)),
            updated_at=str(body.get("updatedAt", "")),
            refs=tuple(str(ref) for ref in body.get("refs", ())),
            # A service that does not report per-repository refs leaves this empty, and
            # `lag()` reads empty as *stale* — silence about a revision is not a claim that
            # it is current.
            refs_by_repo={
                str(slug): str(ref)
                for slug, ref in (body.get("refsByRepo") or {}).items()
                if slug and ref
            },
        )


def _repo(slug: str) -> RepoRef:
    parts = slug.split("/")
    if len(parts) != 3:
        raise ValueError(f"an index answered with {slug!r}, which is not host/namespace/name")
    return RepoRef(*parts)


class HttpIndexWriter(_Wire):
    """`IndexWriter` over the same wire as the reader.

    For a long time `IndexWriter` was a contract with no client: the only code
    that ever wrote to an index was a test doing a raw ``POST /index``. That
    makes "the index learns about a new commit" a claim about a service nobody
    drove, which is why this class exists before any ingestion scheduling does.

    It refuses a repository the caller may not write *before* sending anything,
    for the same reason the reader refuses before it reads: the question "may
    this credential write that repository" belongs to the deployment, not to the
    server's opinion of it.
    """

    __slots__ = ()

    async def apply(self, update: IndexUpdate, constraints: ExecutionConstraints) -> None:
        if not constraints.allows_repo(update.repo):
            raise IndexRefused(
                f"{update.repo.slug} is not one of this principal's repositories, "
                f"so its index may not be written. Nothing was sent."
            )
        await self._request(
            "POST",
            "/index",
            {
                "repo": update.repo.slug,
                "ref": update.ref,
                "documents": [
                    {"path": document.path, "text": document.text} for document in update.documents
                ],
                "deletes": list(update.deletes),
                "complete": update.complete,
                "imports": [{"path": edge.path, "target": edge.target} for edge in update.imports],
            },
        )

    async def indexed_ref(self, repo: RepoRef) -> str | None:
        """Which revision the service last recorded for ``repo``, or ``None``.

        ``None`` is not "the same as HEAD": it means the service has no revision
        for this repository, so an ingestion job has to send a full snapshot
        rather than an empty delta that would look like "nothing changed".
        """
        body = await self._request("GET", "/status", None)
        refs = body.get("refsByRepo") if isinstance(body, dict) else None
        if not isinstance(refs, dict):
            return None
        value = refs.get(repo.slug)
        return str(value) if value else None


class RemoteIndexRetrieverPlugin(PluginService):
    """The enterprise `retriever`: a shared index behind HTTP.

    ``repositories`` is declared in config, not fetched: ``Retriever.repositories``
    is synchronous, and a provider that could only answer it by calling the
    network would make every narrowing decision asynchronous for a list that
    changes per deployment, not per request. A deployment that adds a repository
    adds a line — and if the list is wrong, `search` says the repo is not
    covered rather than pretending it is empty.
    """

    provide = "retriever"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        service_url = str(cfg.get("service_url", "")).strip()
        if not service_url:
            raise ValueError(
                "the remote index provider needs `service_url`: the base URL of the "
                "shared index service. There is deliberately no default — a provider "
                "that guesses would send an enterprise token somewhere unintended."
            )
        self._service_url = service_url
        self._token_env = str(cfg.get("token_env", DEFAULT_TOKEN_ENV))
        self._timeout_s = float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
        self._max_hits = int(cfg.get("max_hits", 200))
        self._repositories = tuple(_repo(str(slug)) for slug in cfg.get("repositories", ()))
        self.denied_calls = 0
        self._transport: httpx.AsyncBaseTransport | None = None

    # -- helpers -----------------------------------------------------------
    def _token(self) -> str:
        return os.environ.get(self._token_env, "")

    def _service(self) -> HttpIndexService:
        return HttpIndexService(
            self._service_url,
            token=self._token(),
            timeout_s=self._timeout_s,
            transport=self._transport,
        )

    def _permitted(self, repos: Any, constraints: ExecutionConstraints) -> tuple[RepoRef, ...]:
        """Narrow to what the caller may read, and refuse what it named explicitly.

        Two different questions, deliberately answered differently:
        a repo the *caller asked for* but may not read is an error (silently
        narrowing it would let a caller believe it was searched); the coverage
        list is intersected silently (it is this deployment's inventory, not the
        caller's request).
        """
        asked = tuple(repos)
        for repo in asked:
            if not constraints.allows_repo(repo):
                self.denied_calls += 1
                raise PermissionError(
                    f"repo {repo.slug!r} is not permitted. Refusing rather than "
                    f"searching the rest and returning a shorter list, which would "
                    f"read as 'there is nothing there'."
                )
        return asked

    # -- Retriever ---------------------------------------------------------
    def capabilities(self) -> RetrieverCapabilities:
        return RetrieverCapabilities(
            supports_symbols=True,
            # This is the one provider that really reads a shared corpus, so it is
            # the one that may say so.
            supports_remote=True,
            # A shared index holds committed revisions; it does not see uncommitted
            # edits, and a provider that claimed otherwise would be the stale-answer
            # defect with a flag on it.
            covers_working_tree=False,
        )

    def repositories(self) -> tuple[RepoRef, ...]:
        return self._repositories

    async def search(self, query: SearchQuery, constraints: ExecutionConstraints) -> list[CodeHit]:
        repos = self._permitted(query.repos, constraints)
        hits = await self._service().search(
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
        repos = self._permitted(query.repos, constraints)
        kinds = [str(kind) for kind in query.kinds]
        hits = await self._service().symbols(
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
        self._permitted((location.repo,), constraints)
        served = await self._service().open(location, span, constraints)
        return FileSlice(
            location=served.location,
            span=served.span,
            text=served.text,
            truncated=served.truncated,
        )

    async def status(self, constraints: ExecutionConstraints) -> IndexStatus:
        return await self._service().status(constraints)


def _is_index_service(candidate: Any) -> bool:
    """Kept for the contract test: `isinstance` on a `runtime_checkable` Protocol."""
    return isinstance(candidate, IndexService)
