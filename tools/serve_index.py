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
import asyncio
import fnmatch
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
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


#: Per-shard budget for one query. The query is fanned out concurrently, so this
#: bounds the whole answer, not each shard's share of a larger budget.
DEFAULT_DEADLINE_MS = 1200

#: `repo` gives one shard per repository; `top-dir` splits a repository by its first
#: path segment, which is what a monorepo needs and what a small repository must not
#: pay for (a dozen one-file shards is slower than one corpus).
SHARD_LAYOUTS = ("repo", "top-dir")


@dataclass(frozen=True, slots=True)
class Shard:
    """One corpus, and the part of a repository it answers for."""

    name: str
    repo: str
    prefix: str
    corpus: IndexCorpus
    layout: str = "repo"


def shards_of(corpus: IndexCorpus, *, by: str = "repo") -> tuple[Shard, ...]:
    """Group a corpus into shards. Deterministic order: by name."""
    if by not in SHARD_LAYOUTS:
        raise ValueError(f"unknown shard layout {by!r}; expected one of {SHARD_LAYOUTS}")
    files: dict[tuple[str, str], list[Any]] = {}
    symbols: dict[tuple[str, str], list[Any]] = {}
    for indexed in corpus.files:
        prefix = (
            indexed.path.split("/", 1)[0] + "/" if by == "top-dir" and "/" in indexed.path else ""
        )
        files.setdefault((indexed.repo.slug, prefix), []).append(indexed)
    for hit in corpus.symbol_hits:
        prefix = (
            hit.location.path.split("/", 1)[0] + "/"
            if by == "top-dir" and "/" in hit.location.path
            else ""
        )
        symbols.setdefault((hit.location.repo.slug, prefix), []).append(hit)
    shards: list[Shard] = []
    for repo, prefix in sorted(set(files) | set(symbols)):
        name = f"{repo}#{prefix.rstrip('/')}" if prefix else repo
        shards.append(
            Shard(
                name=name,
                repo=repo,
                prefix=prefix,
                layout=by,
                corpus=IndexCorpus(
                    tuple(files.get((repo, prefix), ())),
                    tuple(symbols.get((repo, prefix), ())),
                    max_hits=corpus.max_hits,
                ),
            )
        )
    return tuple(shards)


def choose_shards(
    shards: tuple[Shard, ...], repos: tuple[Any, ...], globs: tuple[str, ...] = ()
) -> tuple[tuple[Shard, ...], tuple[Shard, ...]]:
    """Split shards into (relevant, skipped).

    A shard is relevant when its repository was asked for — an empty ``repos`` is
    *no* repository, never all of them, so it selects nothing — and, when globs are
    given, when the shard's prefix could contain a path the glob matches. A shard
    whose prefix does not intersect any glob would only ever return nothing, and
    scanning it is how a fan-out turns into a scan.
    """
    wanted = {repo.slug for repo in repos}
    chosen: list[Shard] = []
    skipped: list[Shard] = []
    for shard in shards:
        if shard.repo not in wanted:
            skipped.append(shard)
            continue
        if globs and not any(_shard_could_hold(shard, g) for g in globs):
            skipped.append(shard)
            continue
        chosen.append(shard)
    return tuple(chosen), tuple(skipped)


def _shard_could_hold(shard: Shard, glob: str) -> bool:
    """Whether a glob could match any path this shard holds.

    The two layouts mean different things by an empty prefix, and conflating them is
    how a fan-out silently loses a shard. Under ``repo`` a shard *is* the repository,
    so it holds every path and is never skipped. Under ``top-dir`` a shard with the
    empty prefix holds only files with no directory at all, so a glob that names one
    (``web/**``) cannot match anything in it — skipping it is exact, not a guess.
    """
    if not shard.prefix:
        return shard.layout != "top-dir" or "/" not in glob
    return _prefix_could_match(shard.prefix, glob)


def _prefix_could_match(prefix: str, glob: str) -> bool:
    """Whether a glob could match any path under ``prefix``.

    Deliberately conservative in the safe direction: a false positive costs one
    shard scan, a false negative is a missing answer.
    """
    stem = glob.split("*", 1)[0]
    if not stem:
        return True
    return stem.startswith(prefix) or prefix.startswith(stem) or fnmatch.fnmatch(prefix, f"{stem}*")


async def fan_out(
    shards: tuple[Shard, ...],
    ask: Any,
    *,
    deadline_s: float,
) -> tuple[list[Any], list[dict[str, str]], tuple[str, ...]]:
    """Ask every shard concurrently; return (answers, failures, answered names).

    A shard that times out or raises is **reported**, never dropped: a partial answer
    presented as a complete one is the confidently-wrong answer this seam exists to
    prevent. The caller decides — the client turns a non-empty failure list into
    `IndexUnavailable` rather than an empty result.
    """

    async def one(shard: Shard) -> tuple[str, Any]:
        return shard.name, await asyncio.wait_for(ask(shard.corpus, shard), timeout=deadline_s)

    results = await asyncio.gather(*(one(shard) for shard in shards), return_exceptions=True)
    answers: list[Any] = []
    failures: list[dict[str, str]] = []
    answered: list[str] = []
    for shard, outcome in zip(shards, results, strict=True):
        if isinstance(outcome, BaseException):
            reason = (
                f"{type(outcome).__name__}: {outcome}"
                if not isinstance(outcome, asyncio.TimeoutError)
                else f"no answer within {deadline_s:.3f}s"
            )
            failures.append({"shard": shard.name, "reason": reason})
            continue
        answered.append(shard.name)
        answers.extend(outcome[1])
    return answers, failures, tuple(answered)


def _deadline(body: Mapping[str, Any], state: Any) -> float:
    """The per-shard budget, from the request when it asks for one.

    Clamped rather than trusted: a client that sends ``deadlineMs: 0`` would otherwise
    turn every shard into a timeout, and one that sends a day would make the deadline
    a decoration.
    """
    raw = body.get("deadlineMs")
    if raw is None:
        return float(state.deadline_s)
    try:
        milliseconds = float(raw)
    except (TypeError, ValueError):
        milliseconds = float(DEFAULT_DEADLINE_MS)
    return max(1.0, min(milliseconds, 30_000.0)) / 1000.0


async def _maybe_slow(app: Any, shard: str) -> None:
    """The one test hook: a named shard takes this long, so a timeout is exercised."""
    delay = app.state.shard_delay_s.get(shard, 0.0)
    if delay:
        await asyncio.sleep(delay)


def build_app(
    corpus: IndexCorpus,
    *,
    tokens: Mapping[str, frozenset[str] | None],
    writers: frozenset[str] = frozenset(),
    updated_at: str = "",
    anonymous: bool = False,
    shard_by: str = "repo",
    shard_delay_s: Mapping[str, float] | None = None,
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
    app.state.shard_by = shard_by
    app.state.deadline_s = DEFAULT_DEADLINE_MS / 1000.0
    # A test hook, and only that: naming a shard here makes it take this long to answer,
    # so "a shard that misses the deadline is reported" is exercised without a real hang.
    app.state.shard_delay_s = dict(shard_delay_s or {})

    app.state.shards = shards_of(corpus, by=shard_by)

    def _rebuild() -> None:
        files = tuple(app.state.files[key] for key in sorted(app.state.files))
        symbols: list[SymbolHit] = []
        for key in sorted(app.state.symbols):
            symbols.extend(app.state.symbols[key])
        app.state.corpus = IndexCorpus(files, symbols)
        app.state.shards = shards_of(app.state.corpus, by=shard_by)

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
        query = SearchQuery(
            pattern=pattern,
            repos=tuple(repos),
            ref=ref,
            globs=tuple(str(g) for g in body.get("globs", ())),
            max_hits=int(body.get("maxHits", 200)),
            literal=bool(body.get("literal", False)),
        )
        chosen, skipped = choose_shards(app.state.shards, tuple(repos), query.globs)
        deadline_s = _deadline(body, app.state)

        async def ask(shard_corpus: Any, shard: Any) -> list[Any]:
            await _maybe_slow(app, shard.name)
            return await shard_corpus.search(query, constraints)

        found, failed, answered = await fan_out(chosen, ask, deadline_s=deadline_s)
        found.sort(
            key=lambda hit: (-hit.score, hit.location.repo.slug, hit.location.path, hit.line)
        )
        return {
            "shards": {
                "answered": list(answered),
                "failed": failed,
                "skipped": [shard.name for shard in skipped],
            },
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
                for hit in found[: query.max_hits]
            ],
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
        query = SymbolQuery(
            name=name,
            repos=tuple(repos),
            ref=ref,
            kinds=kinds,
            max_hits=int(body.get("maxHits", 50)),
        )
        chosen, skipped = choose_shards(app.state.shards, tuple(repos))
        deadline_s = _deadline(body, app.state)

        async def ask(shard_corpus: Any, shard: Any) -> list[Any]:
            await _maybe_slow(app, shard.name)
            return await shard_corpus.symbols(query, constraints)

        found, failed, answered = await fan_out(chosen, ask, deadline_s=deadline_s)
        return {
            "shards": {
                "answered": list(answered),
                "failed": failed,
                "skipped": [shard.name for shard in skipped],
            },
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
                for hit in found[: query.max_hits]
            ],
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
        wanted = CodeRef(repo=repo, path=path, ref=str(body.get("ref", "")))
        span = LineSpan(start, end)
        candidates = sorted(
            (shard for shard in app.state.shards if shard.repo == repo.slug),
            key=lambda shard: -len(shard.prefix),
        )
        served = None
        missing: FileNotFoundError | None = None
        for shard in candidates:
            if shard.prefix and not path.startswith(shard.prefix):
                continue
            try:
                served = await shard.corpus.open(wanted, span, constraints)
            except FileNotFoundError as exc:
                missing = exc
                continue
            break
        if served is None:
            raise Refused(404, str(missing) if missing else f"no shard holds {path}")
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
            "shardBy": app.state.shard_by,
            "shards": [
                {
                    "shard": shard.name,
                    "repo": shard.repo,
                    "prefix": shard.prefix,
                    "files": len(shard.corpus.files),
                    "refs": sorted({indexed.ref for indexed in shard.corpus.files}),
                }
                for shard in app.state.shards
            ],
        }

    return app


def _parse_delays(specs: list[str]) -> dict[str, float]:
    delays: dict[str, float] = {}
    for spec in specs:
        shard, _, seconds = spec.partition("=")
        if not shard or not seconds:
            raise ValueError(f"--shard-delay {spec!r} must be SHARD=SECONDS")
        delays[shard] = float(seconds)
    return delays


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
    parser.add_argument(
        "--shard-by",
        choices=SHARD_LAYOUTS,
        default="repo",
        help="how the corpus is split: one shard per repository, or per top directory",
    )
    parser.add_argument(
        "--shard-deadline-ms",
        type=float,
        default=float(DEFAULT_DEADLINE_MS),
        help=f"per-shard budget for one query by default (default {DEFAULT_DEADLINE_MS})",
    )
    parser.add_argument(
        "--shard-delay",
        action="append",
        default=[],
        metavar="SHARD=SECONDS",
        help=(
            "make one shard take this long before answering; a test hook, so a shard "
            "that misses the deadline is exercised without a real hang"
        ),
    )
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
        shard_by=args.shard_by,
        shard_delay_s=_parse_delays(args.shard_delay),
    )
    app.state.deadline_s = max(0.001, args.shard_deadline_ms / 1000.0)

    import uvicorn

    print(
        f"serving {len(corpus.files)} file(s) in {len(app.state.shards)} shard(s) "
        f"({args.shard_by}) from {args.index} on "
        f"http://{args.host}:{args.port}"
        + (" (anonymous: every repository is readable)" if args.allow_anonymous else ""),
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover - the entry point
    raise SystemExit(main())
