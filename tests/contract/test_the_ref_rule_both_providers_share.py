"""The ref rule lives in the seam because two providers must share it.

It was not there to begin with, and the consequence is the reason these tests exist: the index
provider was given the labelling behaviour while the mock kept the `!=` filter, **and no test
pinned either**, so nothing would have caught the next divergence. The rule is a *labelling*
rule — the provider serves what it has and every hit reports the revision it really came from —
and its absence is not a strict answer but a wrong one:

    `code_search` asks for WORKING_TREE (the agent is editing a checkout)
    an index holds committed revisions (ref="main")
    "no hits" is then indistinguishable from "the file is not there"

The last test is the one that would have caught the drift: both providers, one question, the
same answer.
"""

from __future__ import annotations

import pytest

from forgepilot.seams.refs import PrincipalId, RepoRef, TenantId
from forgepilot.seams.retriever import WORKING_TREE, SearchQuery, ref_to_serve
from forgepilot.seams.security import (
    ExecutionConstraints,
    NetworkAccess,
    Operation,
    Principal,
)

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")


def constraints_for(*repos: RepoRef) -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset(repos),
        permitted_operations=frozenset({Operation.READ_CODE}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


def test_the_requested_ref_wins_when_it_is_held() -> None:
    assert ref_to_serve({"main", "release/1.0"}, "release/1.0") == "release/1.0"


def test_the_fallback_is_deterministic() -> None:
    """Not "whichever the corpus listed first": two runs of one task must agree.

    ⚠️ **Which ref wins is arbitrary-but-stable, and that is deliberate.** The seam prefers
    trunk for nobody: a hardcoded "main" would be wrong for a repository that calls it
    `master`, and choosing a default branch is a *policy* that belongs to whoever configures
    the index, not to the interface. What the rule owes the caller is that the answer does not
    change between runs — and that every hit says which revision it came from.
    """
    held = {"release/1.0", "main", "feature/x"}
    assert ref_to_serve(held, WORKING_TREE) == "feature/x"
    # Same set, different iteration order: set ordering must not decide the revision served.
    assert ref_to_serve(iter(held), WORKING_TREE) == ref_to_serve(held, WORKING_TREE)


def test_holding_nothing_is_an_error() -> None:
    """It cannot answer "which revision" about an empty set, and a default would invent one."""
    with pytest.raises(ValueError, match="no revisions"):
        ref_to_serve(set(), "main")


async def test_both_providers_answer_the_same_question_the_same_way() -> None:
    """The drift test. One asks the working tree of a corpus that holds `main`; both label."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    query = SearchQuery(pattern="retry_login", repos=(REPO,), ref=WORKING_TREE)

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.retrieve.mock:MockRetrieverPlugin"), {}
    )
    try:
        root.retriever.add_file(REPO, "src/login.py", "def retry_login():\n    pass\n")
        mocked = await root.retriever.search(query, constraints_for(REPO))
    finally:
        await dispose_fiber(fiber)

    assert [hit.location.ref for hit in mocked] == ["main"], (
        "the mock still filters on the requested ref, so a tool query reaches nothing — "
        "the same structural zero-hits failure the index provider was fixed for"
    )
