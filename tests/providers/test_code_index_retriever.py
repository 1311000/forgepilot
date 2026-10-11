"""The second provider behind `Retriever` — and it answers for code that is not on disk.

That is the property the seam exists for and the one a local provider cannot have. The
target deployment is 300+ GB behind a metadata-only checkout: a global search there is not a
directory walk, and `find-the-rule`'s own docstring records what a local arm can and cannot
measure — "`no-search` removes `code_search`, not the ability to search: `run` is still there
and `findstr /s` does the same job for about the same number of calls". A provider whose
answers a shell can also reach is not pricing retrieval.

Four things are pinned here because the decision named them, and because a fake is exactly
where they get skipped "because it is only a fake":

- **every hit carries the ref it came from**, so a caller can detect "I asked for my branch
  and got something else";
- **it re-checks `ExecutionConstraints` itself**, counting denials rather than asserting them;
- **it does not overclaim `capabilities()`** — `supports_remote` is `False`, deliberately;
- **a missing or malformed index is a configuration error**, not an empty corpus. "No hits"
  from a provider that never loaded is indistinguishable from "there is nothing there".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgepilot.providers.retrieve.index import CodeIndexRetrieverPlugin
from forgepilot.seams.refs import PrincipalId, RepoRef, TenantId
from forgepilot.seams.retriever import (
    WORKING_TREE,
    Retriever,
    SearchQuery,
    SymbolKind,
    SymbolQuery,
)
from forgepilot.seams.security import (
    ExecutionConstraints,
    NetworkAccess,
    Operation,
    Principal,
)

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")
OTHER = RepoRef(host="git.corp", namespace="team", name="svc-b")

RUNS_PY = (
    "RUN_LENGTH = 3\n"
    "\n"
    "\n"
    "def violated(candidate: str) -> bool:\n"
    "    return 'must not repeat a character 3 times'\n"
)

INDEX = {
    "files": [
        {
            "repo": "git.corp/team/svc-a",
            "path": "policy/rules/runs.py",
            "ref": "main",
            "text": RUNS_PY,
        }
    ],
    "symbols": [
        {
            "repo": "git.corp/team/svc-a",
            "path": "policy/rules/runs.py",
            "ref": "main",
            "name": "violated",
            "kind": "function",
            "start": 4,
            "end": 5,
            "signature": "def violated(candidate: str) -> bool",
        }
    ],
}


def constraints_for(*repos: RepoRef) -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset(repos),
        permitted_operations=frozenset({Operation.READ_CODE}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


def an_index(tmp_path: Path, payload: object = INDEX) -> Path:
    path = tmp_path / "index.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
async def retriever(tmp_path: Path):
    """Built the way a profile builds it, so this exercises the deployment's own path."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.retrieve.index:CodeIndexRetrieverPlugin"),
        {"index_path": str(an_index(tmp_path))},
    )
    try:
        yield root.retriever
    finally:
        await dispose_fiber(fiber)


def test_it_satisfies_the_seam(retriever: CodeIndexRetrieverPlugin) -> None:
    assert isinstance(retriever, Retriever)
    assert retriever.repositories() == (REPO,)


async def test_it_answers_for_code_that_is_nowhere_on_disk(
    retriever: CodeIndexRetrieverPlugin, tmp_path: Path
) -> None:
    """The distinguishing behaviour, and the reason the provider exists at all."""
    hits = await retriever.search(
        SearchQuery(pattern="must not repeat", repos=(REPO,), ref="main"),
        constraints_for(REPO),
    )

    assert len(hits) == 1
    assert hits[0].location.path == "policy/rules/runs.py"
    assert hits[0].line == 5
    # Nothing in the index was ever written to a checkout: a `grep` here finds nothing, and
    # that is the whole difference between measuring retrieval and measuring a directory walk.
    assert not (tmp_path / "policy").exists()


async def test_open_slices_by_span_and_carries_the_ref(
    retriever: CodeIndexRetrieverPlugin,
) -> None:
    from forgepilot.seams.refs import CodeRef, LineSpan

    location = CodeRef(repo=REPO, path="policy/rules/runs.py", ref="main")
    slice_ = await retriever.open(location, LineSpan(1, 1), constraints_for(REPO))

    assert slice_.text == "RUN_LENGTH = 3"
    assert slice_.location.ref == "main"
    assert slice_.truncated is False


async def test_it_serves_what_it_holds_and_labels_it(
    retriever: CodeIndexRetrieverPlugin,
) -> None:
    """⚠️ **The ref is a preference, not a filter — and the tool asks for the working tree.**

    `code_search` sends `WORKING_TREE`, because the agent is editing a checkout, while an index
    holds committed revisions. Under a plain `!=` filter every query here would come back empty:
    structurally, and indistinguishable from "the file is not there". The seam's rule is a
    *labelling* one — a hit may come from trunk while the developer is on a branch — so the
    provider serves what it has and the hit says which revision that was.
    """
    hits = await retriever.search(
        SearchQuery(pattern="must not repeat", repos=(REPO,), ref=WORKING_TREE),
        constraints_for(REPO),
    )

    assert len(hits) == 1
    assert hits[0].location.ref == "main", "the hit must name the revision it really came from"


async def test_it_prefers_the_requested_ref_when_it_has_it(tmp_path: Path) -> None:
    """Two revisions of one file: asking for one must not quietly mix in the other."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    payload = {
        "files": [
            {"repo": "git.corp/team/svc-a", "path": "a.py", "ref": "main", "text": "one\n"},
            {
                "repo": "git.corp/team/svc-a",
                "path": "a.py",
                "ref": "release/1.0",
                "text": "two\n",
            },
        ]
    }
    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.retrieve.index:CodeIndexRetrieverPlugin"),
        {"index_path": str(an_index(tmp_path, payload))},
    )
    try:
        asked = await root.retriever.search(
            SearchQuery(pattern="o", repos=(REPO,), ref="release/1.0"), constraints_for(REPO)
        )
        assert [hit.location.ref for hit in asked] == ["release/1.0"]

        # The fallback is per repo and deterministic, so two runs of one task serve the same
        # revision rather than whichever the index happened to list first.
        fell_back = await root.retriever.search(
            SearchQuery(pattern="o", repos=(REPO,), ref=WORKING_TREE), constraints_for(REPO)
        )
        assert [hit.location.ref for hit in fell_back] == ["main"]
    finally:
        await dispose_fiber(fiber)


async def test_it_re_checks_constraints_itself(
    retriever: CodeIndexRetrieverPlugin,
) -> None:
    """The seam says a provider re-checks rather than trusting its caller: a tool bug must
    not be able to become a data leak."""
    hits = await retriever.search(
        SearchQuery(pattern="must not repeat", repos=(REPO,), ref="main"),
        constraints_for(OTHER),
    )
    assert list(hits) == []
    assert retriever.denied_repo_requests == 1

    from forgepilot.seams.refs import CodeRef, LineSpan

    with pytest.raises(PermissionError):
        await retriever.open(
            CodeRef(repo=REPO, path="policy/rules/runs.py", ref="main"),
            LineSpan(1, 1),
            constraints_for(OTHER),
        )


async def test_an_empty_repo_set_is_not_every_repo(
    retriever: CodeIndexRetrieverPlugin,
) -> None:
    """`()` means "no repositories". Reading it as "all" would be an authorisation bypass."""
    hits = await retriever.search(
        SearchQuery(pattern="must not repeat", repos=(), ref="main"), constraints_for(REPO)
    )
    assert list(hits) == []
    assert retriever.denied_repo_requests == 0


def test_capabilities_do_not_overclaim(retriever: CodeIndexRetrieverPlugin) -> None:
    """⚠️ **`supports_remote` is `False`, and that is the decision, not an oversight.**

    In this codebase "remote" names the shared enterprise index that does not exist on this
    machine. A fake answering `True` would let a composition decide that capability had been
    provided — which is the one thing the decision forbids a fake from doing. The other two
    are the truth: the index declares its own symbols, and it describes committed revisions.
    """
    capabilities = retriever.capabilities()

    assert capabilities.supports_symbols is True
    assert capabilities.supports_remote is False
    assert capabilities.covers_working_tree is False


async def test_symbols_are_served_from_the_index(
    retriever: CodeIndexRetrieverPlugin,
) -> None:
    found = await retriever.symbols(
        SymbolQuery(name="viol", repos=(REPO,), ref="main"), constraints_for(REPO)
    )
    assert [hit.name for hit in found] == ["violated"]
    assert found[0].signature == "def violated(candidate: str) -> bool"

    # The kind filter is honoured, not decorative.
    assert (
        await retriever.symbols(
            SymbolQuery(name="viol", repos=(REPO,), ref="main", kinds=(SymbolKind.CLASS,)),
            constraints_for(REPO),
        )
        == []
    )


def test_a_missing_index_is_a_configuration_error(tmp_path: Path) -> None:
    """Not an empty corpus: a provider that never loaded must not answer "nothing there"."""
    with pytest.raises(FileNotFoundError, match="does not exist"):
        CodeIndexRetrieverPlugin(None, {"index_path": str(tmp_path / "absent.json")})


def test_a_config_without_an_index_path_is_refused() -> None:
    with pytest.raises(ValueError, match="index_path"):
        CodeIndexRetrieverPlugin(None, {})


def test_a_malformed_entry_names_the_entry(tmp_path: Path) -> None:
    path = an_index(tmp_path, {"files": [{"repo": "git.corp/team/svc-a"}]})

    with pytest.raises(ValueError, match=r"files\[0\]"):
        CodeIndexRetrieverPlugin(None, {"index_path": str(path)})


def test_a_slug_that_is_not_three_parts_says_so(tmp_path: Path) -> None:
    path = an_index(
        tmp_path, {"files": [{"repo": "stats", "path": "a.py", "ref": "main", "text": "x"}]}
    )

    with pytest.raises(ValueError, match="host>/<namespace>/<name"):
        CodeIndexRetrieverPlugin(None, {"index_path": str(path)})
