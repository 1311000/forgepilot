"""In-memory retriever — service ``retriever``.

Holds a small corpus in memory so fixtures are explicit and tests are
deterministic. Two behaviours here are the point of the provider rather than
incidental:

- It **re-checks constraints itself**. Repos the caller may not read are skipped
  even when they were asked for explicitly, and every skip is counted. That
  count is what the M5 test asserts on: a tool bug must not become a leak, so
  the check cannot live only in the caller.
- Every hit and slice it returns **carries its ref**, so a caller can detect
  "I asked for my branch and got something else".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef
from forgepilot.seams.retriever import (
    SERVICE_NAME,
    CodeHit,
    FileSlice,
    RetrieverCapabilities,
    SearchQuery,
    SymbolHit,
    SymbolKind,
    SymbolQuery,
)
from forgepilot.seams.security import ExecutionConstraints

__all__ = ["MockRetrieverPlugin", "CorpusFile"]


@dataclass(frozen=True, slots=True)
class CorpusFile:
    repo: RepoRef
    path: str
    text: str
    ref: str = "main"


class MockRetrieverPlugin(PluginService):
    """Serves ``retriever`` from an in-memory corpus."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self.max_hits: int = int(cfg.get("max_hits", 200))
        self._files: list[CorpusFile] = []
        self._symbols: list[SymbolHit] = []
        #: Repos skipped because the caller's constraints did not permit them.
        #: Exposed so tests can prove the provider enforces permissions rather
        #: than assuming the caller did.
        self.denied_repo_requests: int = 0

    # -- fixtures ----------------------------------------------------------
    def add_file(
        self, repo: RepoRef, path: str, text: str, ref: str = "main"
    ) -> MockRetrieverPlugin:
        self._files.append(CorpusFile(repo=repo, path=path, text=text, ref=ref))
        return self

    def add_symbol(
        self,
        repo: RepoRef,
        path: str,
        name: str,
        kind: SymbolKind,
        span: LineSpan,
        ref: str = "main",
        signature: str | None = None,
    ) -> MockRetrieverPlugin:
        self._symbols.append(
            SymbolHit(
                location=CodeRef(repo=repo, path=path, ref=ref),
                name=name,
                kind=kind,
                span=span,
                signature=signature,
            )
        )
        return self

    def _permitted(self, repos: Sequence[RepoRef], constraints: ExecutionConstraints) -> list[RepoRef]:
        permitted: list[RepoRef] = []
        for repo in repos:
            if constraints.allows_repo(repo):
                permitted.append(repo)
            else:
                self.denied_repo_requests += 1
        return permitted

    # -- Retriever ---------------------------------------------------------
    def capabilities(self) -> RetrieverCapabilities:
        return RetrieverCapabilities(
            supports_symbols=True,
            supports_remote=False,
            covers_working_tree=True,
        )

    def repositories(self) -> tuple[RepoRef, ...]:
        """The repos the seeded corpus covers. Covers, not a grant."""
        seen: dict[RepoRef, None] = {}
        for file in self._files:
            seen.setdefault(file.repo, None)
        return tuple(seen)

    async def search(
        self, query: SearchQuery, constraints: ExecutionConstraints
    ) -> Sequence[CodeHit]:
        permitted = set(self._permitted(query.repos, constraints))
        if not permitted:
            return ()

        pattern = re.escape(query.pattern) if query.literal else query.pattern
        try:
            matcher = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid search pattern {query.pattern!r}: {exc}") from exc

        limit = min(query.max_hits, self.max_hits)
        hits: list[CodeHit] = []
        for entry in self._files:
            if entry.repo not in permitted or entry.ref != query.ref:
                continue
            if query.globs and not any(
                entry.path.endswith(g.lstrip("*")) for g in query.globs
            ):
                continue
            for line_no, line in enumerate(entry.text.splitlines(), start=1):
                match = matcher.search(line)
                if match is None:
                    continue
                hits.append(
                    CodeHit(
                        location=CodeRef(repo=entry.repo, path=entry.path, ref=entry.ref),
                        line=line_no,
                        column=match.start() + 1,
                        text=line,
                        score=1.0,
                    )
                )
                if len(hits) >= limit:
                    return hits
        return hits

    async def symbols(
        self, query: SymbolQuery, constraints: ExecutionConstraints
    ) -> Sequence[SymbolHit]:
        permitted = set(self._permitted(query.repos, constraints))
        if not permitted:
            return ()

        needle = query.name.lower()
        found = [
            hit
            for hit in self._symbols
            if hit.location.repo in permitted
            and hit.location.ref == query.ref
            and needle in hit.name.lower()
            and (not query.kinds or hit.kind in query.kinds)
        ]
        return found[: query.max_hits]

    async def open(
        self, location: CodeRef, span: LineSpan, constraints: ExecutionConstraints
    ) -> FileSlice:
        if not constraints.allows_repo(location.repo):
            self.denied_repo_requests += 1
            raise PermissionError(
                f"repo {location.repo.slug!r} is not permitted by the caller's constraints"
            )

        for entry in self._files:
            if (
                entry.repo == location.repo
                and entry.path == location.path
                and entry.ref == location.ref
            ):
                lines = entry.text.splitlines()
                start = max(span.start - 1, 0)
                end = min(span.end, len(lines))
                return FileSlice(
                    location=location,
                    span=span,
                    text="\n".join(lines[start:end]),
                    truncated=end < span.end,
                )

        raise FileNotFoundError(
            f"no fixture for {location.repo.slug}:{location.path}@{location.ref}"
        )
