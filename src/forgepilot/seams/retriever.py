"""Code-retrieval seam — service name ``retriever``.

Two things about this seam are load-bearing rather than incidental:

1. **Permission is enforced here, not only by the caller.** ``search`` receives
   repos already narrowed by the caller's constraints, and a provider is
   *expected to re-check* before touching anything. A tool bug must not be able
   to become a data leak.
2. **Every hit and every slice carries the ref it came from.** In a
   large-repository setting a hit may come from a remote index at trunk while
   the developer is on a branch; a location that does not name its revision is
   not safe to edit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef
from forgepilot.seams.security import ExecutionConstraints

__all__ = [
    "SERVICE_NAME",
    "SearchQuery",
    "CodeHit",
    "SymbolKind",
    "SymbolQuery",
    "SymbolHit",
    "FileSlice",
    "RetrieverCapabilities",
    "Retriever",
]

SERVICE_NAME = "retriever"


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """A search request.

    ``repos`` is the *already-narrowed* set. An empty tuple means "no
    repositories", never "all repositories" — the distinction matters, because
    the latter would be a silent authorisation bypass.

    ``ref`` has **no default**, for the same reason :class:`CodeRef` does not:
    a search that does not say which revision it means cannot have its results
    trusted, and a default of ``"HEAD"`` would quietly mean "trunk" to a
    developer who is working on a branch.
    """

    pattern: str
    repos: tuple[RepoRef, ...]
    ref: str
    globs: tuple[str, ...] = ()
    max_hits: int = 200
    literal: bool = False


class SymbolKind(StrEnum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    INTERFACE = "interface"
    VARIABLE = "variable"
    CONSTANT = "constant"
    MODULE = "module"


@dataclass(frozen=True, slots=True)
class SymbolQuery:
    name: str
    repos: tuple[RepoRef, ...]
    ref: str
    kinds: tuple[SymbolKind, ...] = ()
    max_hits: int = 100


@dataclass(frozen=True, slots=True)
class CodeHit:
    location: CodeRef
    line: int
    column: int
    text: str
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class SymbolHit:
    location: CodeRef
    name: str
    kind: SymbolKind
    span: LineSpan
    signature: str | None = None


@dataclass(frozen=True, slots=True)
class FileSlice:
    """A contiguous read, tagged with the revision it actually came from.

    ``ref`` is what lets a caller detect "I asked for my branch and got trunk".
    """

    location: CodeRef
    span: LineSpan
    text: str
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class RetrieverCapabilities:
    supports_symbols: bool = False
    supports_remote: bool = False
    #: Whether results reflect the working tree (uncommitted edits) rather than a
    #: committed revision.
    covers_working_tree: bool = False


@runtime_checkable
class Retriever(Protocol):
    def capabilities(self) -> RetrieverCapabilities: ...

    async def search(
        self, query: SearchQuery, constraints: ExecutionConstraints
    ) -> Sequence[CodeHit]: ...

    async def symbols(
        self, query: SymbolQuery, constraints: ExecutionConstraints
    ) -> Sequence[SymbolHit]: ...

    async def open(
        self, location: CodeRef, span: LineSpan, constraints: ExecutionConstraints
    ) -> FileSlice: ...
