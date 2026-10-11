"""A configured code index — service ``retriever``.

⚠️ **The one provider here that answers for code which is not in the local checkout.** That
is the whole reason it exists: a `Retriever` whose answers a `grep` can also reach is not
measuring retrieval. `find-the-rule`'s own docstring records the limit of a local arm —
"`no-search` removes `code_search`, not the ability to search: `run` is still there and
`findstr /s` does the same job for about the same number of calls". The target deployment is
300+ GB behind a metadata-only checkout, where a local scan is not merely slow but harmful;
this provider is the *shape* of that answer with the service replaced by a file.

⚠️ **It is deliberately not the deleted `retriever-remote` row.** That row pointed at a module
nobody wrote, and it survived because `--dump-config` never imports and `--boot` skips disabled
rows: a *claim*, not a placeholder. This is a module with behaviour and a contract test, which
is the only thing that tells the two apart.

⚠️ **What it must not claim.** `capabilities()` reports `supports_remote=False` even though it
serves content from outside the checkout: in this codebase "remote" names the shared enterprise
index that does not exist here, and a fake answering `True` would let a composition decide that
capability had been provided. The other two are the truth about this provider —
`supports_symbols=True`, because the index declares its own symbols, and
`covers_working_tree=False`, because it describes committed revisions. That last one is why
every hit carries its ref: a hit can come from a revision the caller is not working on. **So
the requested ref is a preference, not a filter** — `code_search` asks for `WORKING_TREE`
because the agent is editing a checkout, and a provider that dropped every entry whose ref
differed would answer "it is not there" about a file it is holding. See `_refs_to_serve`.

**Loading is strict and eager.** A malformed index raises at boot naming the file and the
entry, for the same reason a malformed ticket source does — a provider that silently serves a
subset of its corpus answers "there is nothing there" to a question it never really asked.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
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

__all__ = ["CodeIndexRetrieverPlugin", "load_index"]


@dataclass(frozen=True, slots=True)
class IndexedFile:
    repo: RepoRef
    path: str
    ref: str
    text: str


def _repo_from_slug(slug: str, *, where: str) -> RepoRef:
    """``git.local/demo/stats`` as a `RepoRef`, the same three-part split five providers use.

    A slug that is not three parts is a configuration error, not a repo with an odd name, and
    saying so here is what stops it becoming "no hits" three calls later.
    """
    parts = slug.split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"{where}: repo {slug!r} is not <host>/<namespace>/<name>")
    return RepoRef(host=parts[0], namespace=parts[1], name=parts[2])


def load_index(path: Path) -> tuple[tuple[IndexedFile, ...], tuple[SymbolHit, ...]]:
    """Read ``path`` into files and symbols. Every defect names the entry it is in."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(
            f"the code index {path} does not exist. A retriever configured without one would "
            f"answer every query with no hits, which is indistinguishable from an empty corpus."
        ) from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"the code index {path} is not valid JSON: {exc}") from None

    files: list[IndexedFile] = []
    for position, entry in enumerate(raw.get("files") or ()):
        where = f"{path}: files[{position}]"
        for key in ("repo", "path", "ref", "text"):
            if key not in entry:
                raise ValueError(f"{where} has no {key!r}")
        files.append(
            IndexedFile(
                repo=_repo_from_slug(str(entry["repo"]), where=where),
                path=str(entry["path"]),
                ref=str(entry["ref"]),
                text=str(entry["text"]),
            )
        )

    symbols: list[SymbolHit] = []
    for position, entry in enumerate(raw.get("symbols") or ()):
        where = f"{path}: symbols[{position}]"
        for key in ("repo", "path", "ref", "name", "kind", "start", "end"):
            if key not in entry:
                raise ValueError(f"{where} has no {key!r}")
        try:
            kind = SymbolKind(str(entry["kind"]))
        except ValueError:
            raise ValueError(
                f"{where}: {entry['kind']!r} is not one of "
                f"{[k.value for k in SymbolKind]}"
            ) from None
        symbols.append(
            SymbolHit(
                location=CodeRef(
                    repo=_repo_from_slug(str(entry["repo"]), where=where),
                    path=str(entry["path"]),
                    ref=str(entry["ref"]),
                ),
                name=str(entry["name"]),
                kind=kind,
                span=LineSpan(int(entry["start"]), int(entry["end"])),
                signature=entry.get("signature"),
            )
        )

    return tuple(files), tuple(symbols)


class CodeIndexRetrieverPlugin(PluginService):
    """Serves ``retriever`` from a configured index rather than from a checkout."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        # Configuration is read **before** the service is initialised, deliberately: a missing
        # or malformed index is a configuration error, and surfacing one should not require a
        # kernel context. It also means the same failure whether a container built this
        # provider or a person did.
        cfg = dict(config or {})
        self.max_hits: int = int(cfg.get("max_hits", 200))
        configured = cfg.get("index_path")
        if not configured:
            raise ValueError(
                "the code index provider needs `index_path`. Without it there is no corpus, "
                "and defaulting to one would be inventing a deployment's data."
            )
        self.index_path = Path(str(configured))
        self._files, self._symbols = load_index(self.index_path)

        super().__init__(ctx)
        #: Repos skipped because the caller's constraints did not permit them. The seam says a
        #: provider re-checks rather than trusting its caller, and a count is how that is shown
        #: rather than asserted.
        self.denied_repo_requests: int = 0

    # -- Retriever ---------------------------------------------------------
    def capabilities(self) -> RetrieverCapabilities:
        return RetrieverCapabilities(
            supports_symbols=True,
            supports_remote=False,
            covers_working_tree=False,
        )

    def repositories(self) -> tuple[RepoRef, ...]:
        """What the index **covers**. Not a grant: the caller still intersects."""
        seen: dict[RepoRef, None] = {}
        for indexed in self._files:
            seen.setdefault(indexed.repo, None)
        for symbol in self._symbols:
            seen.setdefault(symbol.location.repo, None)
        return tuple(seen)

    def _permitted(
        self, repos: Sequence[RepoRef], constraints: ExecutionConstraints
    ) -> set[RepoRef]:
        permitted: set[RepoRef] = set()
        for repo in repos:
            if constraints.allows_repo(repo):
                permitted.add(repo)
            else:
                self.denied_repo_requests += 1
        return permitted

    def _refs_to_serve(
        self, permitted: set[RepoRef], requested: str
    ) -> set[tuple[RepoRef, str]]:
        """The ``(repo, ref)`` pairs to search: what was asked for where it exists, else held.

        ⚠️ **The seam's own sentence, rather than a convenience.** It says a hit "may come from
        a remote index at trunk while the developer is on a branch; a location that does not
        name its revision is not safe to edit" — which is a *labelling* rule, not a filtering
        one. `code_search` asks for `WORKING_TREE`, an index holds committed revisions, and a
        plain `!=` filter would make every query empty: "no hits" from a provider that has the
        file is the one answer a caller cannot tell from "it is not there".

        It **prefers** what was asked for: where the index holds the requested ref for a repo,
        only those entries are served, so one result set never mixes revisions. The fallback is
        per repo and deterministic (the lexicographically first ref held) so two runs of the
        same task serve the same revision — and every hit still says which one that was.
        """
        available: dict[RepoRef, set[str]] = {}
        for indexed in self._files:
            if indexed.repo in permitted:
                available.setdefault(indexed.repo, set()).add(indexed.ref)
        for symbol in self._symbols:
            if symbol.location.repo in permitted:
                available.setdefault(symbol.location.repo, set()).add(symbol.location.ref)

        served: set[tuple[RepoRef, str]] = set()
        for repo, refs in available.items():
            served.add((repo, requested if requested in refs else sorted(refs)[0]))
        return served

    async def search(
        self, query: SearchQuery, constraints: ExecutionConstraints
    ) -> Sequence[CodeHit]:
        # An empty `repos` is "no repositories", never "all of them" — the seam is explicit,
        # because the other reading is a silent authorisation bypass.
        permitted = self._permitted(query.repos, constraints)
        if not permitted:
            return ()

        pattern = re.escape(query.pattern) if query.literal else query.pattern
        try:
            matcher = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid search pattern {query.pattern!r}: {exc}") from exc

        limit = min(query.max_hits, self.max_hits)
        served = self._refs_to_serve(permitted, query.ref)
        hits: list[CodeHit] = []
        for indexed in self._files:
            if indexed.repo not in permitted or (indexed.repo, indexed.ref) not in served:
                continue
            if query.globs and not any(
                indexed.path.endswith(glob.lstrip("*")) for glob in query.globs
            ):
                continue
            for line_no, line in enumerate(indexed.text.splitlines(), start=1):
                match = matcher.search(line)
                if match is None:
                    continue
                hits.append(
                    CodeHit(
                        location=CodeRef(
                            repo=indexed.repo, path=indexed.path, ref=indexed.ref
                        ),
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
        permitted = self._permitted(query.repos, constraints)
        if not permitted:
            return ()

        needle = query.name.lower()
        served = self._refs_to_serve(permitted, query.ref)
        found = [
            hit
            for hit in self._symbols
            if hit.location.repo in permitted
            and (hit.location.repo, hit.location.ref) in served
            and needle in hit.name.lower()
            and (not query.kinds or hit.kind in query.kinds)
        ]
        return found[: min(query.max_hits, self.max_hits)]

    async def open(
        self, location: CodeRef, span: LineSpan, constraints: ExecutionConstraints
    ) -> FileSlice:
        if not constraints.allows_repo(location.repo):
            self.denied_repo_requests += 1
            raise PermissionError(
                f"repo {location.repo.slug!r} is not permitted by the caller's constraints"
            )

        for indexed in self._files:
            if (
                indexed.repo == location.repo
                and indexed.path == location.path
                and indexed.ref == location.ref
            ):
                lines = indexed.text.splitlines()
                start = max(span.start - 1, 0)
                end = min(span.end, len(lines))
                return FileSlice(
                    location=location,
                    span=span,
                    text="\n".join(lines[start:end]),
                    truncated=end < span.end,
                )

        raise FileNotFoundError(
            f"the index has no {location.repo.slug}:{location.path}@{location.ref}"
        )
