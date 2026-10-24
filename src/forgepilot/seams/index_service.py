"""The shared code index — the service a deployment supplies, not a file it ships.

`docs/notes/retrieval.md` and `ForgePilot文档.md` describe the target as
**remote-index-first**: 300+ GB across repositories, a checkout holding metadata
only, contents fetched on demand. `providers/retrieve/index.py` is a *file* that
answers from a corpus somebody built; it is the shape of the answer, and it is
not the service. This seam is what a deployment has to satisfy to be that
service, and it exists so the gap is an interface rather than a paragraph.

Three rules, and each one is here because its absence is a defect this repository
has already recorded once:

1. **Permission is re-checked here.** `search` and `open` receive the caller's
   `ExecutionConstraints`; a provider *narrows with them and refuses with them*,
   and a caller's own filtering is defence in depth rather than the boundary. A
   shared index spans repositories a given developer may not read; a tool bug
   must not become a leak.
2. **Every answer names its revision and its freshness.** A shared index is
   updated per commit and can be behind. A slice that does not say *which* ref it
   served and *when* the index was built is a confidently wrong answer, which is
   the failure mode `ref_to_serve` was written about.
3. **"I cannot answer" is not "nothing there".** A refused query, an unindexed
   repository and a stale index are three different answers; collapsing them into
   an empty result is how a search silently misses most of the code.

The import edges (`IndexImport`, `importers`) are deliberately the *cheap* ones: the
module or package a file names in its static import/require statements, nothing inferred.
No call graph, no name resolution, no dynamic imports — each of those needs a language
front end per language and, done approximately, produces a confident wrong answer about
who depends on what. "This file names this module" is a fact the parser can be sure of.

The wire shape is the provider's business, not the seam's: this module defines
what a caller may ask and what any answer must carry. `providers/retrieve/remote.py`
is one implementation over HTTP, and its module docstring is the protocol it
speaks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef
from forgepilot.seams.retriever import CodeHit, SymbolHit
from forgepilot.seams.security import ExecutionConstraints

__all__ = [
    "SERVICE_NAME",
    "IndexQuery",
    "IndexSymbolQuery",
    "IndexSlice",
    "IndexStatus",
    "IndexService",
    "IndexDocument",
    "IndexUpdate",
    "IndexImport",
    "IndexImportQuery",
    "IndexImporter",
    "imports_match",
    "IndexWriter",
    "IndexLag",
    "lag",
    "IndexRefused",
    "IndexUnavailable",
]

SERVICE_NAME = "indexService"


@dataclass(frozen=True, slots=True)
class IndexQuery:
    """A search over the shared corpus.

    ``repos`` is the *scope the caller may read*, already narrowed by the caller
    and re-checked by the service. An empty tuple means "nothing", never "all" —
    the same rule the retriever seam states, because the two are asked the same
    question by the same tools.
    """

    pattern: str
    repos: Sequence[RepoRef]
    ref: str
    globs: Sequence[str] = ()
    max_hits: int = 200
    literal: bool = False


@dataclass(frozen=True, slots=True)
class IndexSymbolQuery:
    name: str
    repos: Sequence[RepoRef]
    ref: str
    kind: str | None = None
    max_hits: int = 50


@dataclass(frozen=True, slots=True)
class IndexSlice:
    """One file's text, with the two facts a reader needs to trust it."""

    location: CodeRef
    span: LineSpan
    text: str
    truncated: bool = False
    #: When the index that answered was last updated (ISO-8601). Empty means the
    #: service did not say, which a caller should treat as "unknown", not "now".
    indexed_at: str = ""


@dataclass(frozen=True, slots=True)
class IndexStatus:
    """What the corpus covers, so an operator can see staleness without guessing."""

    repositories: int
    files: int
    updated_at: str
    refs: tuple[str, ...] = field(default_factory=tuple)
    #: Slug → the ref the index holds for that repository. Empty for a service that does
    #: not say, which `lag()` treats as **stale**, never as "current".
    refs_by_repo: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IndexDocument:
    """One file's full text, as the index should hold it."""

    path: str
    text: str


@dataclass(frozen=True, slots=True)
class IndexUpdate:
    """One repository's files at one ref — a **snapshot** or a **delta**.

    ``complete=True`` means "this is the whole repository at ``ref``": anything the index
    holds for that repository and that is not in ``documents`` is removed. ``complete=False``
    is a delta: documents are upserted, ``deletes`` are removed, and everything else stays.

    The two are separate because "the commit touched one file" and "here is the tree" are
    opposite statements, and a service that guesses between them either deletes a repository
    or keeps a file that no longer exists.
    """

    repo: RepoRef
    ref: str
    documents: Sequence[IndexDocument] = ()
    deletes: Sequence[str] = ()
    complete: bool = False
    #: The static imports the documents name. A path sent in ``documents`` with no
    #: entry here has its edges *cleared* — a file that stopped importing anything
    #: must stop being an importer, and "absent" is the only way to say that.
    imports: Sequence[IndexImport] = ()


@dataclass(frozen=True, slots=True)
class IndexImport:
    """One static import statement, as the index holds it.

    ``target`` is the specifier the file names, verbatim: a dotted module
    (``astropy.modeling``), a package (``react``), a relative path (``./util``) for
    JavaScript, an import path (``github.com/x/y``) for Go. Relative specifiers are kept
    as written and only match the same string — resolving them needs the module system,
    which is the line this layer does not cross.
    """

    path: str
    target: str


@dataclass(frozen=True, slots=True)
class IndexImportQuery:
    """Who imports this module or package?

    ``module`` is a dotted name or a package prefix. ``astropy.modeling`` matches
    ``astropy.modeling.separable`` (a component prefix), and never ``astropy.modelingx``:
    the match is on whole components, because a substring match would make ``os`` match
    every ``os.path`` question and every ``pygments`` answer.

    Empty ``repos`` is *no repository*, never all of them — the same rule as search.
    """

    module: str
    repos: Sequence[RepoRef]
    ref: str = ""
    max_hits: int = 100


@dataclass(frozen=True, slots=True)
class IndexImporter:
    """One file that names the module, and which specifier it used."""

    location: CodeRef
    target: str


def imports_match(target: str, module: str) -> bool:
    """Whether an imported ``target`` is ``module`` or something inside it.

    One definition, because the service, the client and any second implementation have to
    agree about the one question this layer answers; two readings of ``astropy.modeling``
    would make the edge list disagree with itself.
    """
    return target == module or target.startswith(module + ".")


@runtime_checkable
class IndexWriter(Protocol):
    """The write side. Consumers never see it; an ingestion job does.

    It takes `ExecutionConstraints` like the read side, because "may this caller write this
    repository's entries" is the same kind of question as "may it read them", and a writer
    that trusted its caller would be the leak the read side forbids.
    """

    async def apply(
        self, update: IndexUpdate, constraints: ExecutionConstraints
    ) -> IndexStatus: ...


@dataclass(frozen=True, slots=True)
class IndexLag:
    """Whether the index is behind ``current_ref``, and by what."""

    behind: bool
    indexed_ref: str
    current_ref: str
    detail: str = ""


def lag(status: IndexStatus, repo: RepoRef, current_ref: str) -> IndexLag:
    """Compare what the index holds for ``repo`` with the ref the caller sees.

    Three answers, and the middle one is the reason this is a function rather than a
    sentence: a service that does not report a per-repository ref is **stale**, not fresh.
    "The index did not say" and "the index is current" are different facts, and only one of
    them justifies trusting an answer.
    """
    slug = repo.slug
    if not status.refs_by_repo:
        return IndexLag(
            behind=True,
            indexed_ref="",
            current_ref=current_ref,
            detail="the service did not report which ref it holds, so freshness is unknown",
        )
    indexed = status.refs_by_repo.get(slug, "")
    if not indexed:
        return IndexLag(
            behind=True,
            indexed_ref="",
            current_ref=current_ref,
            detail=f"{slug} is not in this index at all",
        )
    if indexed != current_ref:
        return IndexLag(
            behind=True,
            indexed_ref=indexed,
            current_ref=current_ref,
            detail=f"the index holds {indexed} while the checkout is at {current_ref}",
        )
    return IndexLag(behind=False, indexed_ref=indexed, current_ref=current_ref)


class IndexRefused(PermissionError):
    """The service refused this caller, and says so rather than answering empty."""


class IndexUnavailable(RuntimeError):
    """The service could not be reached, or could not answer just now.

    ⚠️ **Distinct from "no hits" on purpose.** A search that returns nothing
    because the network is down and one that returns nothing because the code is
    not indexed lead to opposite decisions: the first should be retried, the
    second should be reported to whoever owns the index.
    """


@runtime_checkable
class IndexService(Protocol):
    """What a deployment's shared index must implement."""

    async def search(
        self, query: IndexQuery, constraints: ExecutionConstraints
    ) -> Sequence[CodeHit]: ...

    async def symbols(
        self, query: IndexSymbolQuery, constraints: ExecutionConstraints
    ) -> Sequence[SymbolHit]: ...

    async def open(
        self, location: CodeRef, span: LineSpan, constraints: ExecutionConstraints
    ) -> IndexSlice: ...

    async def status(self, constraints: ExecutionConstraints) -> IndexStatus: ...

    async def importers(
        self, query: IndexImportQuery, constraints: ExecutionConstraints
    ) -> Sequence[IndexImporter]: ...
