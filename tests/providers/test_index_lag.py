"""P15: staleness is a pure function, so both providers answer it the same way."""

from __future__ import annotations

from forgepilot.seams.index_service import IndexStatus, lag
from forgepilot.seams.refs import RepoRef


def test_lag_names_a_different_ref() -> None:
    status = IndexStatus(
        repositories=1,
        files=2,
        updated_at="2026-10-21T00:00:00+00:00",
        refs_by_repo={"a/b/c": "old"},
    )
    verdict = lag(status, RepoRef("a", "b", "c"), "new")
    assert verdict.behind
    assert verdict.indexed_ref == "old"
    assert "holds old while the checkout is at new" in verdict.detail


def test_lag_treats_silence_as_stale_never_as_fresh() -> None:
    """ "The index did not say" and "the index is current" are different facts."""
    status = IndexStatus(repositories=1, files=2, updated_at="")
    assert lag(status, RepoRef("a", "b", "c"), "new").behind


def test_lag_says_nothing_when_the_refs_agree() -> None:
    status = IndexStatus(repositories=1, files=2, updated_at="", refs_by_repo={"a/b/c": "same"})
    assert lag(status, RepoRef("a", "b", "c"), "same").behind is False


def test_lag_of_an_unindexed_repository_says_so() -> None:
    status = IndexStatus(repositories=1, files=2, updated_at="", refs_by_repo={"a/b/other": "same"})
    verdict = lag(status, RepoRef("a", "b", "c"), "same")
    assert verdict.behind
    assert "not in this index at all" in verdict.detail
