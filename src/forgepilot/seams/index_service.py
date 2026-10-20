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

The wire shape is the provider's business, not the seam's: this module defines
what a caller may ask and what any answer must carry. `providers/retrieve/remote.py`
is one implementation over HTTP, and its module docstring is the protocol it
speaks.
"""

from __future__ import annotations

from collections.abc import Sequence
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
