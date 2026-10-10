"""The local grep retriever: real search over a real checkout.

This is the default. It is what Claude Code does and what the target environment
does *not* want as its only option — but a working local search is the thing that
makes the seam testable at all, and the enterprise index plugs in behind the same
Protocol.

The tests use the real ``ripgrep`` binary. Substituting a Python reimplementation
would be a different search with a different regex dialect and different glob
semantics, and "a text search and an AST parse are not the same evidence" applies
just as much to two text searches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgepilot.seams.refs import PrincipalId, RepoRef, TenantId
from forgepilot.seams.retriever import WORKING_TREE, SearchQuery
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")
OTHER = RepoRef(host="git.corp", namespace="team", name="svc-b")

SOURCES = {
    "src/login.py": (
        "MAX_ATTEMPTS = 3\n\n\n"
        "def retry_login(client, credentials):\n"
        "    for _ in range(MAX_ATTEMPTS):\n"
        "        try:\n"
        "            return client.login(credentials)\n"
        "        except AuthError:\n"
        "            continue\n"
    ),
    "src/billing.py": "def charge(client):\n    return client.charge()\n",
    "tests/test_login.py": "def test_retry():\n    assert retry_login\n",
}


def constraints_for(*repos: RepoRef) -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset(repos),
        permitted_operations=frozenset({Operation.READ_CODE}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, str]:
    for slug, name in ((REPO.slug, "svc-a"), (OTHER.slug, "svc-b")):
        root = tmp_path / name
        for relative, content in SOURCES.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    return {REPO.slug: str(tmp_path / "svc-a"), OTHER.slug: str(tmp_path / "svc-b")}


@pytest.fixture
async def retriever(roots):
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.retrieve.grep:GrepRetrieverPlugin"),
        {"roots": roots},
    )
    try:
        yield root.retriever
    finally:
        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# It finds things
# ---------------------------------------------------------------------------
async def test_a_search_returns_real_matches_with_their_locations(retriever) -> None:
    hits = await retriever.search(
        SearchQuery(pattern="retry_login", repos=(REPO,), ref="main"),
        constraints_for(REPO),
    )

    assert hits, "a symbol that is plainly in the file was not found"
    assert all(h.location.path for h in hits)
    assert all(h.line > 0 for h in hits)
    assert any("def retry_login" in h.text for h in hits)


async def test_a_hit_carries_the_ref_it_came_from(retriever) -> None:
    """The seam's rule: a location that does not name its revision is not safe
    to edit. A remote hit may be trunk while the developer is on a branch."""
    hits = await retriever.search(
        SearchQuery(pattern="MAX_ATTEMPTS", repos=(REPO,), ref="main"),
        constraints_for(REPO),
    )

    assert hits
    assert all(h.location.ref for h in hits), "a hit with no ref cannot be trusted"
    assert all(h.location.repo == REPO for h in hits)


async def test_no_match_is_an_empty_result_not_an_error(retriever) -> None:
    hits = await retriever.search(
        SearchQuery(pattern="this_string_is_not_in_the_tree", repos=(REPO,), ref="main"),
        constraints_for(REPO),
    )

    assert hits == ()


async def test_globs_narrow_the_search(retriever) -> None:
    everywhere = await retriever.search(
        SearchQuery(pattern="retry_login", repos=(REPO,), ref="main"),
        constraints_for(REPO),
    )
    only_src = await retriever.search(
        SearchQuery(pattern="retry_login", repos=(REPO,), ref="main", globs=("src/**",)),
        constraints_for(REPO),
    )

    assert len(only_src) < len(everywhere), "the glob did not narrow anything"
    assert all(h.location.path.startswith("src/") for h in only_src)


# ---------------------------------------------------------------------------
# It enforces permission itself
# ---------------------------------------------------------------------------
async def test_a_repository_the_caller_cannot_reach_is_refused_and_counted(retriever) -> None:
    """The seam re-checks rather than trusting its caller.

    Searches *and* reads: a tool bug must not be able to become a data leak, and a
    provider where the check is "someone else's job" is where one caller
    eventually forgets. The refusal is counted so a test can assert it happened
    rather than infer it from an empty result.
    """
    before = retriever.denied_calls

    with pytest.raises(PermissionError, match="not permitted"):
        await retriever.search(
            SearchQuery(pattern="retry_login", repos=(OTHER,), ref="main"),
            constraints_for(REPO),  # alice may read REPO, not OTHER
        )

    assert retriever.denied_calls == before + 1


async def test_a_repository_with_no_checkout_is_an_error_not_an_empty_result(retriever) -> None:
    """'I searched nothing' and 'there was nothing' must not look alike.

    Returning ``()`` for a repository the provider cannot reach is the most
    dangerous possible answer: it is indistinguishable from a genuine no-match,
    so a caller concludes the symbol does not exist.
    """
    from forgepilot.providers.retrieve.grep import UnknownCheckout

    unconfigured = RepoRef(host="git.corp", namespace="team", name="not-checked-out")

    with pytest.raises(UnknownCheckout, match="not-checked-out"):
        await retriever.search(
            SearchQuery(pattern="x", repos=(unconfigured,), ref="main"),
            constraints_for(REPO, unconfigured),
        )


# ---------------------------------------------------------------------------
# open()
# ---------------------------------------------------------------------------
async def test_open_returns_the_lines_it_actually_read(retriever) -> None:
    from forgepilot.seams.refs import CodeRef, LineSpan

    hits = await retriever.search(
        SearchQuery(pattern="def retry_login", repos=(REPO,), ref="main"),
        constraints_for(REPO),
    )
    location = hits[0].location

    slice_ = await retriever.open(
        location, LineSpan(start=1, end=4), constraints_for(REPO)
    )

    assert "MAX_ATTEMPTS = 3" in slice_.text
    assert slice_.location.ref == location.ref, "the slice must report the ref it read"
    assert slice_.location.path == location.path


async def test_open_refuses_a_path_that_escapes_the_checkout(retriever, tmp_path) -> None:
    """A traversal in a `CodeRef` is a request to read outside the repository."""
    from forgepilot.seams.refs import CodeRef, LineSpan

    outside = tmp_path / "secret.txt"
    outside.write_text("not yours\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="outside"):
        await retriever.open(
            CodeRef(repo=REPO, path="../secret.txt", ref="main"),
            LineSpan(start=1, end=1),
            constraints_for(REPO),
        )


async def test_open_refuses_a_repository_the_caller_cannot_reach(retriever) -> None:
    from forgepilot.seams.refs import CodeRef, LineSpan

    with pytest.raises(PermissionError, match="not permitted"):
        await retriever.open(
            CodeRef(repo=OTHER, path="src/login.py", ref="main"),
            LineSpan(start=1, end=1),
            constraints_for(REPO),
        )


# ---------------------------------------------------------------------------
# The ref a hit carries is resolved, not echoed
# ---------------------------------------------------------------------------
async def test_the_ref_is_read_from_the_checkout_not_copied_from_the_query(
    roots, tmp_path
) -> None:
    """Asking for `main` and reporting `main` proves nothing.

    The seam's rule is that a hit names the revision it *came from*. A provider
    that echoes the query would satisfy every inspection and still be wrong: a
    caller on a branch who receives trunk results has no way to notice, and edits
    the wrong version of a file.
    """
    import subprocess

    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root_path = Path(roots[REPO.slug])
    subprocess.run(["git", "init", "-b", "main"], cwd=root_path, capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
        cwd=root_path, capture_output=True, check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root_path, capture_output=True, text=True, check=True
    ).stdout.strip()

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.retrieve.grep:GrepRetrieverPlugin"),
        {"roots": roots},
    )
    try:
        clean = await root.retriever.search(
            SearchQuery(pattern="MAX_ATTEMPTS", repos=(REPO,), ref="something-else"),
            constraints_for(REPO),
        )
        assert clean and clean[0].location.ref == head, (
            "the ref must be the commit actually searched, not the one requested"
        )

        # And an uncommitted edit is not part of any commit, so the ref says so.
        (root_path / "src" / "login.py").write_text("MAX_ATTEMPTS = 9\n", encoding="utf-8")
        dirty = await root.retriever.search(
            SearchQuery(pattern="MAX_ATTEMPTS", repos=(REPO,), ref="main"),
            constraints_for(REPO),
        )
        assert dirty[0].location.ref == f"{head}+dirty", (
            "results from a dirty tree were reported as a commit"
        )
    finally:
        await dispose_fiber(fiber)


async def test_a_directory_that_is_not_a_repository_says_so_in_its_ref(retriever) -> None:
    """No revision exists, so none may be invented."""
    from forgepilot.providers.retrieve.grep import WORKTREE

    hits = await retriever.search(
        SearchQuery(pattern="MAX_ATTEMPTS", repos=(REPO,), ref="main"),
        constraints_for(REPO),
    )

    assert hits
    assert hits[0].location.ref == WORKTREE, (
        "a plain directory was reported as being at the requested revision"
    )


# ---------------------------------------------------------------------------
# The working tree wins over the configured path
# ---------------------------------------------------------------------------
async def test_a_search_can_be_pointed_at_the_callers_working_tree(
    retriever, roots, tmp_path: Path
) -> None:
    """**The two are different questions.** A repository lives at a path; a
    session works in a checkout cut from it. After the first edit they are
    different content, and answering the second with the first returns pre-edit
    code that looks exactly like an answer.

    Found the hard way: an agent edited a worktree while `code_search` searched
    the repository it was cut from. On a repo host that difference was total —
    the mirror was bare, so it searched nothing, and "no match" read as "that
    symbol does not exist".
    """
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "login.py").write_text(
        "def retry_login():\n    return 'edited in this session'\n", encoding="utf-8"
    )

    hits = await retriever.search(
        SearchQuery(
            pattern="edited in this session",
            repos=(REPO,),
            ref=WORKING_TREE,
            root=str(worktree),
        ),
        constraints_for(REPO),
    )

    assert hits, "the working tree was not searched"
    assert hits[0].location.path == "src/login.py"
    # And the ref is the *worktree's*, not the configured checkout's.
    assert hits[0].location.repo == REPO


async def test_the_working_tree_does_not_widen_the_grant(
    retriever, roots, tmp_path: Path
) -> None:
    """An override changes which copy is read, never which repositories may be.

    Otherwise pointing `root` at a directory would be a way past the permission
    check — and the check is here, in the provider, precisely because a caller
    eventually forgets.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "secret.py").write_text("the other repository's code\n", encoding="utf-8")

    with pytest.raises(PermissionError):
        await retriever.search(
            SearchQuery(pattern="secret", repos=(OTHER,), ref=WORKING_TREE, root=str(worktree)),
            constraints_for(REPO),  # may read REPO, not OTHER
        )
    assert retriever.denied_calls > 0, "the refusal was not counted"


async def test_a_working_tree_that_is_not_there_is_refused_not_silently_ignored(
    retriever, roots, tmp_path: Path
) -> None:
    """Falling back to the configured checkout would answer a question about the
    working tree with content from somewhere else, which is the whole bug."""
    from forgepilot.providers.retrieve.grep import UnknownCheckout

    with pytest.raises(UnknownCheckout) as caught:
        await retriever.search(
            SearchQuery(
                pattern="retry_login",
                repos=(REPO,),
                ref=WORKING_TREE,
                root=str(tmp_path / "does-not-exist"),
            ),
            constraints_for(REPO),
        )

    assert "not a directory" in str(caught.value)
    assert "falling back" in str(caught.value)
