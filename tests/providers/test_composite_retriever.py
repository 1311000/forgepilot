"""The composite retriever: a corpus with the working tree in front of it.

Two providers already existed and neither was the deployment the design describes. The corpus
answers for code that is not on disk but cannot see an uncommitted edit; the checkout sees
today's file but only inside one repository. `covers_working_tree` is the field that tells a
caller which of the two it is talking to — and until this provider existed, choosing the
corpus meant telling the agent that its own edits had vanished.

These tests pin the three rules the module argues for, because each of them is a way the
composition can be *convenient* and wrong:

- a path that exists on disk is answered from disk (the corpus copy is stale by definition);
- a path the working tree deleted stays deleted (the overlay must not resurrect it);
- every hit names the revision it came from, and the two halves name different ones.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from forgepilot.seams.refs import CodeRef, LineSpan, PrincipalId, RepoRef, TenantId
from forgepilot.seams.retriever import (
    WORKING_TREE,
    Retriever,
    SearchQuery,
    SymbolQuery,
)
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")
OTHER = RepoRef(host="git.corp", namespace="team", name="svc-b")

OLD = "OLD_MARKER = 1\n"
NEW = "NEW_MARKER = 1\n"
GONE = "GONE_MARKER = 1\n"
CONTRACT_PY = "CONTRACT_MARKER = 1\n\n\ndef the_contract():\n    return CONTRACT_MARKER\n"

INDEX = {
    "files": [
        {"repo": REPO.slug, "path": "src/app.py", "ref": "main", "text": OLD},
        {"repo": REPO.slug, "path": "src/gone.py", "ref": "main", "text": GONE},
        {"repo": OTHER.slug, "path": "lib/contract.py", "ref": "main", "text": CONTRACT_PY},
    ],
    "symbols": [
        {
            "repo": OTHER.slug,
            "path": "lib/contract.py",
            "ref": "main",
            "name": "the_contract",
            "kind": "function",
            "start": 4,
            "end": 5,
        },
        {
            "repo": REPO.slug,
            "path": "src/gone.py",
            "ref": "main",
            "name": "gone_marker",
            "kind": "constant",
            "start": 1,
            "end": 1,
        },
    ],
}

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def constraints_for(*repos: RepoRef) -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset(repos),
        permitted_operations=frozenset({Operation.READ_CODE}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


def commit(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@test", "commit", "-m", message],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A real checkout whose working tree disagrees with the corpus, on purpose.

    Committed: ``src/app.py`` holding ``OLD`` and ``src/gone.py``. Then the working tree edits
    ``app.py`` to ``NEW`` and deletes ``gone.py`` — the two states a corpus cannot express and
    an overlay has to.
    """
    root = tmp_path / "svc-a"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(OLD, encoding="utf-8")
    (root / "src" / "gone.py").write_text(GONE, encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    commit(root, "the fixture this example runs against")

    (root / "src" / "app.py").write_text(NEW, encoding="utf-8")
    (root / "src" / "gone.py").unlink()
    return root


def an_index(tmp_path: Path, payload: object = INDEX) -> Path:
    path = tmp_path / "index.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
async def retriever(tmp_path: Path, checkout: Path):
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.retrieve.composite:CompositeRetrieverPlugin"),
        {"index_path": str(an_index(tmp_path)), "roots": {REPO.slug: str(checkout)}},
    )
    try:
        yield root.retriever
    finally:
        await dispose_fiber(fiber)


def test_it_satisfies_the_seam(retriever) -> None:
    assert isinstance(retriever, Retriever)
    assert retriever.repositories() == (REPO, OTHER)


def test_capabilities_say_the_working_tree_is_covered(retriever) -> None:
    """The field that distinguishes this provider from the corpus alone — so it must be true."""
    capabilities = retriever.capabilities()

    assert capabilities.covers_working_tree is True
    assert capabilities.supports_symbols is True
    # Still a file one deployment laid down, not the shared enterprise service.
    assert capabilities.supports_remote is False


async def test_a_composite_with_no_checkouts_does_not_claim_the_working_tree(
    tmp_path: Path,
) -> None:
    """The GVFS shape: a corpus and no checkout is a legal deployment, and the capability
    field is where it has to say so. Claiming `covers_working_tree` with nothing checked out
    would tell a caller its edits are visible when nothing is looking at them."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.retrieve.composite:CompositeRetrieverPlugin"),
        {"index_path": str(an_index(tmp_path)), "roots": {}},
    )
    try:
        assert root.retriever.capabilities().covers_working_tree is False
        hits = await root.retriever.search(
            SearchQuery(pattern="CONTRACT_MARKER", repos=(OTHER,), ref=WORKING_TREE),
            constraints_for(OTHER),
        )
        assert hits, "with no checkout the corpus is still the answer"
    finally:
        await dispose_fiber(fiber)


async def test_the_working_tree_wins_over_a_stale_index_entry(retriever) -> None:
    """The corpus holds ``OLD``, the file on disk says ``NEW``. Answering ``NEW`` with ``OLD``
    is the confidently wrong answer the seam's ref rule exists to make detectable."""
    hits = await retriever.search(
        SearchQuery(pattern="NEW_MARKER", repos=(REPO,), ref=WORKING_TREE),
        constraints_for(REPO),
    )

    assert [hit.text.strip() for hit in hits] == [NEW.strip()]
    assert hits[0].location.ref != "main", "a working-tree hit must not claim the corpus's ref"

    stale = await retriever.search(
        SearchQuery(pattern="OLD_MARKER", repos=(REPO,), ref=WORKING_TREE),
        constraints_for(REPO),
    )
    assert stale == (), "the index's copy of a file on disk is not a second answer"


async def test_a_repository_with_no_checkout_is_answered_from_the_corpus(retriever) -> None:
    hits = await retriever.search(
        SearchQuery(pattern="CONTRACT_MARKER", repos=(OTHER,), ref=WORKING_TREE),
        constraints_for(OTHER),
    )

    assert hits, "a repository only the corpus covers must still be searchable"
    assert hits[0].location.repo == OTHER
    assert hits[0].location.ref == "main", "a corpus hit names the revision it holds"
    assert hits[0].location.root is None


async def test_a_file_the_working_tree_deleted_does_not_come_back(retriever) -> None:
    """Deleted is not absent: the corpus still holds the file, and serving it would undo the
    developer's edit one search at a time."""
    hits = await retriever.search(
        SearchQuery(pattern="GONE_MARKER", repos=(REPO,), ref=WORKING_TREE),
        constraints_for(REPO),
    )

    assert hits == ()
    symbols = await retriever.symbols(
        SymbolQuery(name="gone_marker", ref=WORKING_TREE, repos=(REPO,)), constraints_for(REPO)
    )
    assert symbols == (), "a symbol of a deleted file is a symbol the branch does not have"


async def test_open_reads_the_working_tree_copy(retriever, checkout: Path) -> None:
    location = CodeRef(repo=REPO, path="src/app.py", ref=WORKING_TREE, root=str(checkout))
    slice_ = await retriever.open(location, LineSpan(1, 1), constraints_for(REPO))

    assert slice_.text.strip() == NEW.strip()
    assert slice_.location.ref != "main"


async def test_open_falls_through_to_the_corpus_for_a_repo_with_no_checkout(retriever) -> None:
    location = CodeRef(repo=OTHER, path="lib/contract.py", ref=WORKING_TREE)
    slice_ = await retriever.open(location, LineSpan(1, 1), constraints_for(OTHER))

    assert slice_.text.strip() == CONTRACT_PY.splitlines()[0]
    assert slice_.location.ref == "main"


async def test_open_refuses_a_path_the_working_tree_deleted(retriever, checkout: Path) -> None:
    location = CodeRef(repo=REPO, path="src/gone.py", ref=WORKING_TREE, root=str(checkout))

    with pytest.raises(FileNotFoundError) as caught:
        await retriever.open(location, LineSpan(1, 1), constraints_for(REPO))

    # "the corpus has no such file" would be false, and a caller cannot tell that apart from
    # "you deleted it" — which is what actually happened.
    assert "deleted" in str(caught.value)
    assert "gone.py" in str(caught.value)


async def test_a_denied_repository_is_refused_before_either_half_answers(retriever) -> None:
    with pytest.raises(PermissionError):
        await retriever.search(
            SearchQuery(pattern="CONTRACT_MARKER", repos=(REPO,), ref=WORKING_TREE),
            constraints_for(OTHER),
        )

    assert retriever.denied_calls >= 1


async def test_a_composite_without_an_index_is_a_configuration_error(tmp_path: Path) -> None:
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root

    root = create_root()
    with pytest.raises(ValueError) as caught:
        await root.plugin(
            resolve_plugin(
                "forgepilot.providers.retrieve.composite:CompositeRetrieverPlugin"
            ),
            {"roots": {}},
        )

    assert "index_path" in str(caught.value)
