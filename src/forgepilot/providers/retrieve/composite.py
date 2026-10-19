"""An index with a working tree in front of it — service ``retriever``.

**This is the composition the target design actually describes**, and until it existed the
repository could only *swap* retrieval: `bundles/code_index/patch.yaml` replaced the checkout
with an index, so `covers_working_tree` became `false` and an agent's own uncommitted edits
became invisible to the very tool meant to find code. `docs/notes/retrieval.md` has carried
that as "no composite provider" since M19.

What it combines, and why each half is load-bearing:

- **The corpus** answers for code the checkout does not contain — another repository, a
  revision not fetched, a path that was never materialised. That is the 300 GB/GVFS premise:
  a local scan cannot reach it, and pretending otherwise is the failure the seam warns about.
- **The checkout** answers for *now*: uncommitted edits and the branch's own files. An index
  is a snapshot of commits, so on its own it answers today's question with yesterday's code.
  It also **hides what the working tree deleted** — a path still in the corpus but gone from
  the branch must not come back as a hit, or the overlay would be undoing the developer's
  edit one search at a time.

Three rules make the combination honest rather than merely convenient:

1. **A file that exists on disk is answered from disk.** The corpus copy of that path is
   stale by definition — same path, older content — and serving it would be the "confidently
   wrong answer" the M14 slice fix was about. Only paths the checkout does not have are
   answered from the corpus.
2. **Deleted is not absent.** A path in `HEAD` but missing from disk is a deletion in
   progress: `open` refuses it *and says which of the two it is*, because "the index has no
   such file" would be false and a caller cannot tell that from "you deleted it".
3. **Every hit carries its ref** — `WORKING_TREE` candidates report the commit they were
   actually read from (with `+dirty` when the tree is not clean), corpus hits report the
   revision the corpus holds. One result set, two revisions, each labelled. The seam asks for
   exactly that and the alternative — one ref for both — is a lie about one of them.

Composed by inheritance from the local provider, deliberately: the checkout half *is* that
provider (riprep binary resolution, working-tree refs, the `root` that travels with a hit),
and a second copy of those rules is the drift `ref_to_serve` was created to stop. The corpus
half is `IndexCorpus`, shared with the index provider for the same reason.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from forgepilot.providers.retrieve.grep import GrepRetrieverPlugin, UnknownCheckout
from forgepilot.providers.retrieve.index import IndexCorpus
from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef
from forgepilot.seams.retriever import (
    SERVICE_NAME,
    CodeHit,
    FileSlice,
    RetrieverCapabilities,
    SearchQuery,
    SymbolHit,
    SymbolQuery,
)
from forgepilot.seams.security import ExecutionConstraints

__all__ = ["CompositeRetrieverPlugin"]


class CompositeRetrieverPlugin(GrepRetrieverPlugin):
    """Serves ``retriever`` from a corpus **and** the working tree, in that order of need.

    Config::

        index_path: "/srv/indices/code.json"     # required — no corpus, no composition
        roots:      {"git.corp/team/svc-a": "/srv/checkouts/svc-a"}   # optional
        binary:     "rg"                         # optional
        max_hits:   200                          # optional, caps the corpus side

    ``roots`` is optional on purpose: a deployment whose checkouts are materialised on demand
    may have the corpus and nothing else for most repositories, and requiring a local dir for
    each would force a configuration that lies about what is on disk. A repository with no
    root is simply answered by the corpus alone.
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        cfg = dict(config or {})
        # Configuration before `super().__init__`, because that call registers the provide row:
        # raising afterwards would leave a half-built service visible to the kernel.
        configured = cfg.get("index_path")
        if not configured:
            raise ValueError(
                "the composite retriever needs `index_path`. It exists to put a working tree "
                "in front of a corpus, and without the corpus it would silently be the local "
                "provider with a different name."
            )
        self.corpus = IndexCorpus.from_path(
            Path(str(configured)), max_hits=int(cfg.get("max_hits", 200))
        )
        super().__init__(ctx, cfg)

    # -- capabilities ------------------------------------------------------
    def capabilities(self) -> RetrieverCapabilities:
        return RetrieverCapabilities(
            # The corpus declares symbols; the working tree cannot, and the union is still
            # "symbols are supported" because a caller asking for them gets corpus answers.
            supports_symbols=True,
            # Still not the shared enterprise service — that is what `supports_remote=False`
            # names in this codebase, and this provider serves a file.
            supports_remote=False,
            # True *because of the composition*, and only while there is a checkout to
            # compose with. A deployment whose `roots` is empty has the corpus and nothing
            # else, which is the GVFS shape — so the field says so rather than repeating the
            # class's ambition. This is the field that tells a caller the difference between
            # this provider and `CodeIndexRetrieverPlugin`, so it has to be the truth.
            covers_working_tree=bool(self._roots),
        )

    def repositories(self) -> tuple[RepoRef, ...]:
        """Locals first, then what only the corpus covers. Covers, never a grant."""
        out: list[RepoRef] = list(super().repositories())
        seen = set(out)
        for repo in self.corpus.repositories():
            if repo not in seen:
                out.append(repo)
                seen.add(repo)
        return tuple(out)

    # -- helpers -----------------------------------------------------------
    def _local_root(self, repo: RepoRef, query_root: str | None) -> Path | None:
        """The checkout for ``repo``, or ``None`` when this repo has no local copy.

        ``None`` is not an error here, unlike the local provider: a corpus-only repository is
        the normal case this provider exists for. What is still an error is a **caller-named**
        working tree that is not there — that is a session whose tree moved, and answering it
        from the corpus would be answering a different question.
        """
        if query_root:
            # Reuses `_root`, which refuses a missing working tree rather than falling back.
            return self._root(repo, _PERMISSIVE, query_root)
        if repo.slug in self._roots:
            return self._roots[repo.slug]
        return None

    @staticmethod
    def _at_head(root: Path, path: str) -> bool:
        """Whether ``path`` is in the checked-out commit — i.e. a deletion is what removed it."""
        completed = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{path}"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return completed.returncode == 0

    def _on_disk(self, root: Path | None, path: str) -> bool:
        return root is not None and (root / path).is_file()

    # -- search ------------------------------------------------------------
    async def search(
        self, query: SearchQuery, constraints: ExecutionConstraints
    ) -> Sequence[CodeHit]:
        local: list[CodeHit] = []
        roots: dict[RepoRef, Path | None] = {}
        for repo in query.repos:
            # Authorisation first, for every repo, even the ones only the corpus covers: the
            # corpus re-checks too, but a refused repo must not reach either half.
            if not constraints.allows_repo(repo):
                self.denied_calls += 1
                raise PermissionError(f"repo {repo.slug!r} is not permitted")

            try:
                root = self._local_root(repo, query.root)
            except UnknownCheckout:
                # The caller named a working tree that is gone. Refuse the whole search
                # rather than quietly answering from the corpus — that is a different copy.
                raise
            roots[repo] = root
            if root is None:
                continue

            ref = self._ref(root, query.ref)
            for record in self._run(root, query):
                local.append(self._hit(record, repo, root, ref))
                if len(local) >= query.max_hits:
                    return tuple(local)

        seen = {(hit.location.repo, hit.location.path, hit.line) for hit in local}
        remaining = query.max_hits - len(local)
        if remaining <= 0:
            return tuple(local)

        from_corpus = await self.corpus.search(query, constraints)
        hits = list(local)
        for hit in from_corpus:
            if (hit.location.repo, hit.location.path, hit.line) in seen:
                continue
            root = roots.get(hit.location.repo)
            if root is not None:
                if self._on_disk(root, hit.location.path):
                    # The checkout has this path; its content is newer by definition, and the
                    # local search above already answered for it.
                    continue
                if self._at_head(root, hit.location.path):
                    # Deleted in the working tree: the overlay hides it rather than letting
                    # the corpus resurrect the file the developer just removed.
                    continue
            hits.append(hit)
            seen.add((hit.location.repo, hit.location.path, hit.line))
            if len(hits) >= query.max_hits:
                break
        return tuple(hits)

    # -- symbols -----------------------------------------------------------
    async def symbols(
        self, query: SymbolQuery, constraints: ExecutionConstraints
    ) -> Sequence[SymbolHit]:
        """Corpus only, and the checkout's deletions still apply.

        A symbol table cannot be derived from a working tree without parsing it, and grep does
        not parse — so this half is the corpus's answer, filtered by the same "deleted is not
        absent" rule. Overclaiming here would be a symbol hit for a class the branch removed.
        """
        found = await self.corpus.symbols(query, constraints)
        kept: list[SymbolHit] = []
        for hit in found:
            root = None
            if query.repos:
                for repo in query.repos:
                    if repo == hit.location.repo and repo.slug in self._roots:
                        root = self._roots[repo.slug]
            if root is not None and not self._on_disk(root, hit.location.path):
                if self._at_head(root, hit.location.path):
                    continue
            kept.append(hit)
        return tuple(kept)

    # -- open --------------------------------------------------------------
    async def open(
        self, location: CodeRef, span: LineSpan, constraints: ExecutionConstraints
    ) -> FileSlice:
        root: Path | None
        try:
            root = self._local_root(location.repo, location.root)
        except UnknownCheckout:
            root = None

        if root is not None and self._on_disk(root, location.path):
            # The copy the developer is editing. `GrepRetrieverPlugin.open` also reports the
            # ref it really read and clamps the span to the file's length.
            return await super().open(location, span, constraints)

        if root is not None and self._at_head(root, location.path):
            raise FileNotFoundError(
                f"{location.repo.slug}:{location.path} is in the checked-out commit but not on "
                f"disk, so the working tree deleted it. The corpus still holds a revision of "
                f"it; reading that would undo the deletion."
            )

        # Either no checkout at all, or a path the checkout never had: the corpus is the only
        # place a reader can get it — and its slice names the revision it served.
        return await self.corpus.open(location, span, constraints)


class _Permissive:
    """A constraints stand-in for `_root`'s path resolution only.

    `_root` checks the grant and resolves the caller's working tree in one method. Here the
    grant has already been checked against every repo in the query, so this only has to say
    "yes" to the second half — and it exists as an object rather than a boolean argument
    because changing `_root`'s signature would touch the provider whose behaviour the eval
    matrix just priced.
    """

    def allows_repo(self, repo: RepoRef) -> bool:  # noqa: ARG002 - the answer is always yes
        return True


_PERMISSIVE = _Permissive()
