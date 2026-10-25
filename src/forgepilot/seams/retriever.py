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

import fnmatch
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef
from forgepilot.seams.security import ExecutionConstraints

__all__ = [
    "SERVICE_NAME",
    "WORKING_TREE",
    "SearchQuery",
    "CodeHit",
    "SymbolKind",
    "SymbolQuery",
    "SymbolHit",
    "FileSlice",
    "RetrieverCapabilities",
    "Retriever",
    "matches_glob",
    "ref_to_serve",
    "working_ref_of",
]

SERVICE_NAME = "retriever"

#: A ``SearchQuery.ref`` meaning "whatever is checked out right now".
#:
#: A sentinel rather than a branch name, because the alternative is worse: a
#: caller editing a checkout does not have a revision to name, and passing
#: ``"HEAD"`` would claim a commit while the results actually include uncommitted
#: edits. A provider that honours this reports in each hit the commit it really
#: read — including ``+dirty`` when the tree is not clean.
WORKING_TREE = "<worktree>"


def matches_glob(path: str, glob: str) -> bool:
    """Whether a repository-relative ``path`` matches ``glob``.

    One definition, because two levels speak this language: the shard a query is sent to,
    and the file a hit is kept from. They disagreed once — shard selection treated
    ``api/**`` as a directory, the file filter stripped the leading ``*`` and compared
    suffixes, so a query was sent to the right shard and then threw away every hit it
    answered with. That failure is invisible: an empty result looks like "not there".

    ``**`` matches any number of path components, including none (``api/**`` matches
    ``api/x.py`` and ``api/a/b.py``); everything else is :func:`fnmatch.fnmatchcase`,
    where ``*`` also spans ``/`` — the same forgiving reading ripgrep's ``--glob`` has.
    """
    if glob.startswith("**/"):
        glob = glob[3:]
    if glob == "**":
        return True
    if glob.endswith("/**"):
        prefix = glob[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatchcase(path, glob)


def ref_to_serve(held: Iterable[str], requested: str) -> str:
    """Which revision to serve when the one asked for is not held.

    ⚠️ **A preference, not a filter — and this is the seam's own sentence, not a
    convenience.** The docstring above says a hit "may come from a remote index at trunk while
    the developer is on a branch; a location that does not name its revision is not safe to
    edit". That is a *labelling* rule: the provider serves what it has and every hit reports
    the revision it really came from, so the caller can notice the substitution.

    ⚠️ **The filter reading is a structural failure, not a strict one.** `code_search` asks for
    ``WORKING_TREE`` — the agent is editing a checkout — while an index holds committed
    revisions. A provider that dropped every entry whose ref differed would answer "no hits"
    for a file it is holding, and a caller can never tell that from "it is not there".

    It still **prefers** what was asked for, so one result set never mixes revisions, and the
    fallback is deterministic (the lexicographically first ref held) so two runs of one task
    serve the same revision rather than whichever the corpus happened to list first.

    ⚠️ **It lives in the seam because two providers must agree about it and may not import
    each other.** The rule had already drifted once — the index provider was given the
    labelling behaviour while the mock kept the filter — and no test pinned either, so nothing
    would have caught the next divergence.
    """
    revisions = set(held)
    if not revisions:
        raise ValueError("no revisions are held, so there is nothing to serve")
    return requested if requested in revisions else sorted(revisions)[0]


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

    ``root`` is **where the caller is working**, when it knows. It is not a
    substitute for ``repos``: that still says which repositories are in scope and
    a provider re-checks the grant against it. It says *which copy* to look at,
    and the two are different questions. A repository lives at a path; a session
    works in a checkout cut from it, and the two diverge the moment the session
    edits anything. A provider that greps a tree should prefer this; a provider
    that queries a shared index has no use for it and ignoring it is not an
    error.
    """

    pattern: str
    repos: tuple[RepoRef, ...]
    ref: str
    globs: tuple[str, ...] = ()
    max_hits: int = 200
    literal: bool = False
    root: str | None = None


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

    def repositories(self) -> tuple[RepoRef, ...]:
        """The repositories this provider can address.

        Exists because a caller cannot narrow to repos it has no way to name.
        ``SearchQuery.repos`` must be an already-narrowed set — an empty tuple
        means "no repositories", never "all" — and a model shown only a
        *prefix* (``git.corp/team/*``) has nothing to write into that set except
        a guess.

        What comes back is what the provider **covers**, not what the caller may
        read: the caller still intersects with its constraints. A provider that
        returned everything as a convenience would be moving the authorisation
        decision into the thing being authorised.
        """
        ...

    async def search(
        self, query: SearchQuery, constraints: ExecutionConstraints
    ) -> Sequence[CodeHit]: ...

    async def symbols(
        self, query: SymbolQuery, constraints: ExecutionConstraints
    ) -> Sequence[SymbolHit]: ...

    async def open(
        self, location: CodeRef, span: LineSpan, constraints: ExecutionConstraints
    ) -> FileSlice: ...


async def working_ref_of(
    retriever: Retriever,
    repo: RepoRef,
    constraints: ExecutionConstraints,
    root: str | None = None,
) -> str | None:
    """The revision the *checkout* is on — when the retriever has a checkout at all.

    Optional on purpose, and the optionality is the honest part. A shared index answers
    for the revision it ingested and has no idea what any developer's working tree is at;
    a provider that cannot know says so by not having the method, which is a different
    statement from "the tree is at the revision I served". Conflating the two is how a
    confidently-wrong answer gets its confidence.

    A reader compares this with the ref an answer carried: equal means the answer is
    current, different means the index is behind, and `None` means nobody can tell.

    ``root`` is the caller's working tree when it has one. It matters: the configured
    path is where the repository lives, and a session works in a checkout cut from it,
    so asking the configured copy answers a question about a different tree.
    """
    method = getattr(retriever, "working_ref", None)
    if method is None:
        return None
    return await method(repo, constraints, root)
