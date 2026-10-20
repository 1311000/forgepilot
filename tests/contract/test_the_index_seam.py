"""The index-service seam: the shape a deployment has to meet, and who may meet it.

Two things are asserted here because both are easy to lose silently:

* the HTTP adapter really satisfies the protocol (a `runtime_checkable` protocol
  only checks attribute presence, which is exactly enough to catch a rename), and
* no bundle a **shipped profile** loads points the retriever at a network
  service. Sending an enterprise token is a deployment's decision, not a
  default; a bundle may exist for it (``retriever_remote``), it just may not be
  in the profile people start with.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forgepilot.providers.retrieve.remote import HttpIndexService
from forgepilot.seams.index_service import (
    IndexQuery,
    IndexRefused,
    IndexService,
    IndexSlice,
    IndexStatus,
    IndexSymbolQuery,
    IndexUnavailable,
)
from forgepilot.seams.refs import CodeRef, LineSpan, RepoRef

FORGE = Path(__file__).resolve().parents[2]
PACKAGE = FORGE / "src" / "forgepilot"
BUNDLES = PACKAGE / "bundles"
PROFILES = FORGE / "configs" / "profiles"
REMOTE_MODULE = "forgepilot.providers.retrieve.remote"


def _rows(bundle: str) -> list[dict]:
    loaded = yaml.safe_load((BUNDLES / bundle / "patch.yaml").read_text(encoding="utf-8"))
    return [row for row in loaded or () if isinstance(row, dict)]


def _default_bundles() -> set[str]:
    names: set[str] = set()
    for path in PROFILES.glob("*/profile.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        names.update(data.get("bundles", ()))
    return names


def test_the_http_adapter_satisfies_the_seam():
    assert isinstance(HttpIndexService("https://index.corp/api"), IndexService)


def test_an_empty_repo_set_means_no_repositories():
    """The seam's oldest authorization rule, re-asserted where it is used.

    ``repos=()`` must survive as "nothing", never widened to "everything": the
    difference between the two is a silent permission bypass.
    """
    query = IndexQuery(pattern="x", repos=(), ref="main")

    assert query.repos == ()
    assert not query.repos


def test_no_shipped_profile_reaches_the_network_by_default():
    """A profile people start with must not send an enterprise token anywhere."""
    offenders: list[str] = []
    for bundle in sorted(_default_bundles()):
        for row in _rows(bundle):
            if row.get("disabled"):
                continue
            if REMOTE_MODULE in str(row.get("plugin", "")):
                offenders.append(f"{bundle}: {row.get('id')} -> {row.get('plugin')}")

    assert offenders == [], (
        "these rows are in a shipped profile and reach a network index: "
        f"{offenders}. The remote provider needs a service URL and a token, so it "
        "belongs in a bundle a deployment adds (`retriever_remote`)."
    )


def test_the_default_retriever_is_the_local_one():
    retriever_rows = [row for row in _rows("base") if row.get("id") == "retriever"]
    assert len(retriever_rows) == 1
    assert "providers.retrieve.grep" in str(retriever_rows[0]["plugin"])


def test_the_remote_bundle_exists_and_is_opt_in():
    """The provider ships with the bundle that enables it, or it is unreachable.

    This is the same defect M28 found for `read_code`: a provider nothing can
    select is a provider nobody uses.
    """
    rows = _rows("retriever_remote")

    assert [row["id"] for row in rows] == ["retriever"]
    assert REMOTE_MODULE in str(rows[0]["plugin"])
    assert "retriever_remote" not in _default_bundles()


def test_the_seam_reports_three_different_answers():
    """ "no hits", "not permitted" and "could not ask" are three answers.

    Collapsing them would make an outage look like an empty corpus.
    """
    assert issubclass(IndexUnavailable, RuntimeError)
    # A refusal is a permission error, not an unavailable service: one is fixed
    # by asking someone for access, the other by waiting.
    assert issubclass(IndexRefused, PermissionError)
    assert not issubclass(IndexRefused, IndexUnavailable)


@pytest.mark.parametrize(
    "value",
    [
        IndexSlice(
            location=CodeRef(repo=RepoRef("git.corp", "team", "svc-a"), path="a.py", ref="main"),
            span=LineSpan(1, 2),
            text="x = 1\n",
        ),
        IndexStatus(repositories=1, files=1, updated_at="now"),
        IndexSymbolQuery(name="page", repos=(), ref="main"),
    ],
)
def test_the_value_objects_default_to_the_safe_side(value):
    """Freshness may be absent; ``truncated`` and ``refs`` may not be guessed."""
    if isinstance(value, IndexSlice):
        assert value.indexed_at == ""
        assert value.truncated is False
    if isinstance(value, IndexStatus):
        assert value.refs == ()
