"""A reference index service: the other side of `HttpIndexService`.

Why this exists at all. **Q4** wrote the `IndexService` seam, an HTTP client for it
(`providers/retrieve/remote.py`) and an opt-in bundle — and every test of that client used a
`MockTransport`. All it proved was that our client agrees with our client: the wire shape had
never crossed a socket, so "a deployment's service can meet this interface" was a claim about
a dictionary in a test file. This is a service a deployment can actually start, and
`tests/providers/test_the_index_over_http.py` drives the real client against it over
loopback.

**It is a reference, not the enterprise index.** The corpus is one JSON file produced by
`tools/build_code_index.py` — no incremental per-commit updates, no sharded storage, no
permission service, no semantic index. What it *is* is the protocol, executed: the four
endpoints, the refs, the refusals, and the freshness stamp. A deployment swaps this out and
keeps the client.

Two decisions worth stating:

* **The matcher is `IndexCorpus`, not sqlite/FTS5.** A second matcher is how "the same query"
  starts meaning two different things — the in-process provider and this service would drift
  on substring-vs-token semantics, globbing and `literal`, and every drift would show up as
  "the remote index finds less than the local one". One corpus object, two transports.
* **Authentication is a token → repositories map, and it fails closed.** No configured token
  means the process refuses to start unless `--allow-anonymous` is passed on purpose; an
  unknown token is a 401; a repository outside a token's list is a 403, *not* an empty result
  — "you may not read this" and "there is nothing there" are different answers, and the seam
  says so.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# FastAPI is imported where it is used, but the *handler signatures* below are resolved by
# `get_type_hints` against this module's globals — and with `from __future__ import
# annotations` a `Request` imported inside a function is a string that resolves to nothing,
# so FastAPI reads the parameter as a query field and every POST answers 422. Aliasing the
# class at module scope is what makes the annotation resolve; it is not needed at runtime
# unless a request is served.
try:  # pragma: no cover - the `web` extra is what serves
    from fastapi import Request as _Request
except ImportError:  # pragma: no cover - `--help` and `parse_tokens` work without it
    _Request = Any  # type: ignore[assignment,misc]

from forgepilot.providers.retrieve.index import IndexCorpus, IndexedFile, symbols_for
from forgepilot.seams.index_service import IndexDocument
from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef
from forgepilot.seams.retriever import SearchQuery, SymbolHit, SymbolKind, SymbolQuery
from forgepilot.seams.security import ExecutionConstraints, Operation

__all__ = ["DEFAULT_TOKEN_ENV", "build_app", "parse_tokens"]

#: The *name* of the variable holding a single all-repositories token, never the value.
DEFAULT_TOKEN_ENV = "FORGEPILOT_INDEX_TOKEN"

#: An upper bound on a slice, so `/file` cannot be asked for a whole repository by accident.
MAX_SLICE_LINES = 2000


def _now() -> str:
    """An ISO-8601 UTC stamp. Second resolution: a reader compares, not orders."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class Refused(Exception):
    """Raised by a handler helper when the caller may not be served."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def parse_tokens(specs: list[str]) -> dict[str, frozenset[str] | None]:
    """``alice=*`` means every repository; ``bob=host/ns/a,host/ns/b`` means those two.

    ``None`` as the value is "no restriction", which is why it is spelled ``*`` rather than
    an empty list: an empty allow-list and "anything" are opposite answers and must not share
    a representation (`ExecutionConstraints.allowed_commands` learned the same lesson).
    """
    tokens: dict[str, frozenset[str] | None] = {}
    for spec in specs:
        if "=" not in spec:
            raise ValueError(
                f"--token {spec!r} must be TOKEN=REPOS, for example alice=* or "
                f"bob=git.corp/team/api,git.corp/team/web"
            )
        token, _, repos = spec.partition("=")
        if not token or not repos:
            raise ValueError(f"--token {spec!r} has an empty token or an empty repository list")
        if token in tokens:
            raise ValueError(f"--token names {token!r} twice; the first entry would be lost")
        if repos.strip() == "*":
            tokens[token] = None
            continue
        slugs = frozenset(part.strip() for part in repos.split(",") if part.strip())
        badly_shaped = sorted(slug for slug in slugs if len(slug.split("/")) != 3)
        if badly_shaped:
            raise ValueError(
                f"these repositories in --token {token!r} are not host/namespace/name: "
                f"{badly_shaped}"
            )
        tokens[token] = slugs
    return tokens


def _repo_ref(slug: str) -> RepoRef:
    parts = slug.split("/")
    if len(parts) != 3:
        raise Refused(400, f"{slug!r} is not host/namespace/name")
    return RepoRef(*parts)


def _constraints(
    principal: str, allowed: frozenset[str] | None, corpus: IndexCorpus
) -> ExecutionConstraints:
    """A principal's authority, expressed the way every other provider expects it.

    ``None`` means *every* repository, and it is written as the empty prefix rather
    than resolved against the corpus. Two reasons, both measured:

    * A corpus-derived scope cannot authorise the first snapshot of a repository the
      index does not hold yet, so ingestion of anything new was impossible.
    * It also disappears at the worst moment: once a repository's last file is
      deleted, its slug leaves the corpus, and a ``*`` token then got **403 "you may
      not read this"** where the true answer is "the index holds nothing for it".
      Those two answers lead to opposite decisions, which is the seam's first rule.

    A ``*`` prefix is not a new permission model: ``allows_repo`` already matches
    glob-ish prefixes, and ``*`` is the documented spelling for "anything".
    """
    if allowed is None:
        prefixes = frozenset({"*"})
        slugs: frozenset[str] = frozenset()
    else:
        prefixes = frozenset()
        slugs = allowed
    return ExecutionConstraints(
        principal_id=principal,
        tenant_id="index",
        allowed_repos=frozenset(_repo_ref(slug) for slug in slugs),
        allowed_repo_prefixes=prefixes,
        permitted_operations=frozenset({Operation.READ_CODE}),
    )


def build_app(
    corpus: IndexCorpus,
    *,
    tokens: Mapping[str, frozenset[str] | None],
    writers: frozenset[str] = frozenset(),
    updated_at: str = "",
    anonymous: bool = False,
) -> Any:
    """The four endpoints, bound to one corpus and one token map.

    Imported here rather than at module scope so `--help` works without the `web` extra.
    """
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="ForgePilot reference index", version="0.1.0")
    app.state.corpus = corpus

    app.state.files = {(indexed.repo.slug, indexed.path): indexed for indexed in corpus.files}
    app.state.symbols = {
        (hit.location.repo.slug, hit.location.path): [hit] for hit in corpus.symbol_hits
    }
    app.state.stamps = {indexed.repo.slug: indexed.ref for indexed in corpus.files}
    app.state.updated_at = updated_at or _now()

    def _rebuild() -> None:
        files = tuple(app.state.files[key] for key in sorted(app.state.files))
        symbols: list[SymbolHit] = []
        for key in sorted(app.state.symbols):
            symbols.extend(app.state.symbols[key])
        app.state.corpus = IndexCorpus(files, symbols)

    def _apply(
        repo: RepoRef,
        ref: str,
        documents: list[IndexDocument],
        deletes: tuple[str, ...],
        complete: bool,
    ) -> int:
        """Mutate the snapshot, then rebuild — one transaction's worth of work, in memory.

        `complete` first removes what the repository no longer has, then documents are
        written, so a snapshot never contains a file it just declared gone.
        """
        slug = repo.slug
        if complete:
            keeps = {(slug, document.path) for document in documents}
            for key in [key for key in app.state.files if key[0] == slug and key not in keeps]:
                app.state.files.pop(key, None)
                app.state.symbols.pop(key, None)
        for path in deletes:
            key = (slug, path)
            app.state.files.pop(key, None)
            app.state.symbols.pop(key, None)
        for document in documents:
            key = (slug, document.path)
            app.state.files[key] = IndexedFile(
                repo=repo, path=document.path, ref=ref, text=document.text
            )
            app.state.symbols[key] = list(
                symbols_for(document.text, repo=repo, path=document.path, ref=ref)
            )
        app.state.stamps[slug] = ref
        app.state.updated_at = _now()
        _rebuild()
        return sum(1 for key in app.state.files if key[0] == slug)

    def _who(request: _Request) -> tuple[str, frozenset[str] | None]:
        if anonymous:
            return "anonymous", None
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            raise Refused(401, "this index wants `Authorization: Bearer <token>`")
        for token, allowed in tokens.items():
            # Not constant time. A reference index for a development deployment states this
            # rather than implying a security property it does not have.
            if presented == token:
                return token, allowed
        raise Refused(401, "that token is not known to this index")

    @app.exception_handler(Refused)
    async def _refusal(_request: _Request, exc: Refused) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.post("/search")
    async def search(request: _Request) -> Any:
        principal, allowed = _who(request)
        body = await request.json()
        pattern = str(body.get("pattern", ""))
        if not pattern:
            raise Refused(400, "`pattern` is required; an empty one would match everything")
        ref = str(body.get("ref", ""))
        repos = [_repo_ref(str(slug)) for slug in body.get("repos", ())]
        corpus = app.state.corpus
        constraints = _constraints(principal, allowed, corpus)
        refused = [repo.slug for repo in repos if not constraints.allows_repo(repo)]
        if refused:
            # 403 rather than an empty list: the seam's rule is that "you may not read this"
            # and "there is nothing there" are different answers.
            raise Refused(403, f"this token may not read {refused}")
        hits = await corpus.search(
            SearchQuery(
                pattern=pattern,
                repos=tuple(repos),
                ref=ref,
                globs=tuple(str(g) for g in body.get("globs", ())),
                max_hits=int(body.get("maxHits", 200)),
                literal=bool(body.get("literal", False)),
            ),
            constraints,
        )
        return {
            "hits": [
                {
                    "repo": hit.location.repo.slug,
                    "path": hit.location.path,
                    "ref": hit.location.ref,
                    "line": hit.line,
                    "column": hit.column,
                    "text": hit.text,
                    "score": hit.score,
                }
                for hit in hits
            ]
        }

    @app.post("/symbols")
    async def symbols(request: _Request) -> Any:
        principal, allowed = _who(request)
        body = await request.json()
        name = str(body.get("name", ""))
        if not name:
            raise Refused(400, "`name` is required")
        ref = str(body.get("ref", ""))
        repos = [_repo_ref(str(slug)) for slug in body.get("repos", ())]
        corpus = app.state.corpus
        constraints = _constraints(principal, allowed, corpus)
        refused = [repo.slug for repo in repos if not constraints.allows_repo(repo)]
        if refused:
            raise Refused(403, f"this token may not read {refused}")
        try:
            kinds = tuple(SymbolKind(str(kind)) for kind in body.get("kinds", ()) if kind)
        except ValueError as exc:
            raise Refused(400, str(exc)) from None
        hits = await corpus.symbols(
            SymbolQuery(
                name=name,
                repos=tuple(repos),
                ref=ref,
                kinds=kinds,
                max_hits=int(body.get("maxHits", 50)),
            ),
            constraints,
        )
        return {
            "symbols": [
                {
                    "repo": hit.location.repo.slug,
                    "path": hit.location.path,
                    "ref": hit.location.ref,
                    "name": hit.name,
                    "kind": str(hit.kind),
                    "start": hit.span.start,
                    "end": hit.span.end,
                    "signature": hit.signature,
                }
                for hit in hits
            ]
        }

    @app.post("/file")
    async def file_slice(request: _Request) -> Any:
        principal, allowed = _who(request)
        body = await request.json()
        repo = _repo_ref(str(body.get("repo", "")))
        path = str(body.get("path", ""))
        if not path:
            raise Refused(400, "`path` is required")
        start = max(1, int(body.get("start", 1)))
        end = max(start, int(body.get("end", start + 80 - 1)))
        corpus = app.state.corpus
        constraints = _constraints(principal, allowed, corpus)
        if not constraints.allows_repo(repo):
            raise Refused(403, f"this token may not read {repo.slug}")
        # The bound is the service's, not the caller's: `/file` must not become a way to ask
        # for a repository in one request.
        end = min(end, start + MAX_SLICE_LINES - 1)
        try:
            served = await corpus.open(
                CodeRef(repo=repo, path=path, ref=str(body.get("ref", ""))),
                LineSpan(start, end),
                constraints,
            )
        except FileNotFoundError as exc:
            raise Refused(404, str(exc)) from None
        return {
            "repo": served.location.repo.slug,
            "path": served.location.path,
            "ref": served.location.ref,
            "start": served.span.start,
            "end": served.span.end,
            "text": served.text,
            "truncated": served.truncated,
            "indexedAt": app.state.updated_at,
        }

    @app.post("/index")
    async def index_put(request: _Request) -> Any:
        who, allowed = _who(request)
        if who not in writers:
            # Writing is not reading with a bigger number: a token allowed to read a
            # repository has not thereby been granted the right to replace what everyone
            # else is served. With no writer configured this refuses everything.
            raise Refused(403, f"token {who!r} may not update this index")
        body = await request.json()
        repo = _repo_ref(str(body.get("repo", "")))
        ref = str(body.get("ref", ""))
        if not ref:
            raise Refused(
                400, "`ref` is required: an entry with no revision cannot be checked for staleness"
            )
        constraints = _constraints(who, allowed, app.state.corpus)
        if not constraints.allows_repo(repo):
            raise Refused(403, f"this token may not write {repo.slug}")
        documents: list[IndexDocument] = []
        for position, raw in enumerate(body.get("documents", ())):
            path = str(raw.get("path", ""))
            if not path:
                raise Refused(400, f"documents[{position}] has no path")
            documents.append(IndexDocument(path=path, text=str(raw.get("text", ""))))
        deletes = tuple(str(path) for path in body.get("deletes", ()) if path)
        count = _apply(repo, ref, documents, deletes, bool(body.get("complete", False)))
        return {
            "repo": repo.slug,
            "ref": ref,
            "files": count,
            "refsByRepo": dict(app.state.stamps),
        }

    @app.get("/status")
    async def status(request: _Request) -> Any:
        _who(request)
        corpus = app.state.corpus
        refs = sorted({indexed.ref for indexed in corpus.files})
        return {
            "repositories": len(corpus.repositories()),
            "files": len(corpus.files),
            "updatedAt": app.state.updated_at,
            "refs": refs,
            "refsByRepo": dict(app.state.stamps),
        }

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve an index over the IndexService wire format")
    parser.add_argument(
        "--index", required=True, type=Path, help="a JSON index from tools/build_code_index.py"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument(
        "--token",
        action="append",
        default=[],
        metavar="TOKEN=REPOS",
        help="a token and the repositories it may read (`*` for every one); repeatable",
    )
    parser.add_argument(
        "--token-env",
        default=DEFAULT_TOKEN_ENV,
        help=f"read one all-repositories token from this variable (default {DEFAULT_TOKEN_ENV})",
    )
    parser.add_argument(
        "--allow-anonymous",
        action="store_true",
        help="serve every repository to anyone; refuse-by-default is the point of the token map",
    )
    parser.add_argument(
        "--write-token",
        action="append",
        default=[],
        metavar="TOKEN",
        help="a token allowed to POST /index; repeatable, and none by default",
    )
    parser.add_argument("--updated-at", default="", help="an ISO stamp for /status")
    args = parser.parse_args(argv)

    if not args.index.is_file():
        print(f"error: {args.index} is not there", file=sys.stderr)
        return 2
    tokens = parse_tokens(args.token)
    from_env = os.environ.get(args.token_env, "")
    if from_env and not tokens:
        tokens = {from_env: None}
    if not tokens and not args.allow_anonymous:
        print(
            f"error: no token configured. Pass --token TOKEN=REPOS, set {args.token_env}, "
            f"or pass --allow-anonymous on purpose.",
            file=sys.stderr,
        )
        return 2

    corpus = IndexCorpus.from_path(args.index)
    writers = frozenset(args.write_token)
    for token in writers:
        if token not in tokens:
            # A writer that cannot be authenticated is not a writer. Naming it here is
            # better than a 403 at three in the morning.
            print(
                f"error: --write-token {token!r} is not in the token map; add it with "
                f"--token {token}=REPOS as well",
                file=sys.stderr,
            )
            return 2
    app = build_app(
        corpus,
        tokens=tokens,
        writers=writers,
        updated_at=args.updated_at,
        anonymous=args.allow_anonymous,
    )

    import uvicorn

    print(
        f"serving {len(corpus.files)} file(s) from {args.index} on "
        f"http://{args.host}:{args.port}"
        + (" (anonymous: every repository is readable)" if args.allow_anonymous else ""),
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover - the entry point
    raise SystemExit(main())
