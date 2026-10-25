"""The index says which revision it answered for; the checkout says which one it holds.

P25's claim is narrow and testable: when those two differ, the *answer the model reads*
says so. A note that only appears in a metric nobody scrapes, or in a log line nobody
folds, is the defect shape this project keeps meeting — a field with no consumer.

Three states have to stay distinguishable, and the third is the one that is easy to get
wrong:

* **equal** — the index is current, so there is nothing to say;
* **different** — the index answers for an older revision, so the reader is told;
* **unknown** — a shared index has no checkout of its own, so *no sentence appears*.
  "Nobody could tell" must never render as "it is current".
"""

from __future__ import annotations

import json

import pytest

from forgepilot.runtime.tools.builtin import _code_search, _read_code
from forgepilot.runtime.tools.pipeline import ToolContext
from forgepilot.seams.refs import RepoRef, SessionId, TurnId
from forgepilot.seams.retriever import CodeHit, CodeRef, FileSlice, LineSpan
from forgepilot.seams.security import Operation
from forgepilot.seams.tools import Capabilities, ToolCall
from tests.conftest import constraints_for

REPO = RepoRef(host="git.local", namespace="demo", name="service")


class Indexed:
    """A retriever that holds a revision, with or without a checkout to compare it to."""

    def __init__(self, ref: str, *, working: str | None) -> None:
        self.ref = ref
        self.working = working

    def repositories(self) -> tuple[RepoRef, ...]:
        return (REPO,)

    async def open(self, location: CodeRef, span: LineSpan, constraints: object) -> FileSlice:
        return FileSlice(
            location=CodeRef(repo=REPO, path=location.path, ref=self.ref),
            span=span,
            text="if total > limit:\n",
        )

    async def search(self, query: object, constraints: object) -> tuple[CodeHit, ...]:
        return (
            CodeHit(
                location=CodeRef(repo=REPO, path="service/limits.py", ref=self.ref),
                line=4,
                column=0,
                text="if total > limit:",
            ),
        )

    async def symbols(self, query: object, constraints: object) -> tuple[()]:
        return ()

    async def working_ref(self, repo: RepoRef, constraints: object, root: str | None = None):
        return self.working


class Checkoutless(Indexed):
    """A shared index: it has no checkout, so it cannot answer the question at all."""

    working_ref = None  # type: ignore[assignment]


def _context(tmp_path, *, write: bool = True) -> ToolContext:
    return ToolContext(
        session_id=SessionId("s-1"),
        constraints=constraints_for(Operation.READ_CODE, writable=("src",), repos=[REPO]),
        turn=TurnId(1),
        step=1,
        capabilities=Capabilities(fs_read=True, fs_write=write),
        workspace_root=tmp_path,
    )


def _call(arguments: dict[str, object]) -> ToolCall:
    return ToolCall(call_id="c-1", name="read_code", arguments_json=json.dumps(arguments))


@pytest.mark.asyncio
async def test_a_slice_behind_the_checkout_says_so_in_the_text_the_model_reads(tmp_path):
    retriever = Indexed("aaa111", working="bbb222")
    outcome = await _read_code(_call({"path": "service/limits.py"}), _context(tmp_path), retriever)

    assert outcome.meta["stale"] is True
    assert outcome.meta["ref"] == "aaa111"
    assert outcome.meta["workingRef"] == "bbb222"
    assert "bbb222" in outcome.content
    assert "aaa111" in outcome.content


@pytest.mark.asyncio
async def test_a_slice_at_the_checkout_revision_has_no_note(tmp_path):
    retriever = Indexed("bbb222", working="bbb222")
    outcome = await _read_code(_call({"path": "service/limits.py"}), _context(tmp_path), retriever)

    assert outcome.meta["stale"] is False
    assert "stale" not in outcome.content
    assert "bbb222" in outcome.content.split("\n", 1)[0]


@pytest.mark.asyncio
async def test_an_index_without_a_checkout_says_nothing_and_does_not_claim_freshness(tmp_path):
    retriever = Checkoutless("bbb222", working=None)
    outcome = await _read_code(_call({"path": "service/limits.py"}), _context(tmp_path), retriever)

    assert outcome.meta["stale"] is False
    assert "stale" not in outcome.content
    assert outcome.meta["workingRef"] is None


@pytest.mark.asyncio
async def test_search_says_it_once_per_repository_not_once_per_hit(tmp_path):
    retriever = Indexed("aaa111", working="bbb222")
    outcome = await _code_search(
        _call({"pattern": "total > limit", "output_mode": "content"}),
        _context(tmp_path),
        retriever,
    )

    assert outcome.meta["staleRepos"] == [REPO.slug]
    assert "stale index" in outcome.content
    assert outcome.content.count("the checkout is at") == 1


@pytest.mark.asyncio
async def test_search_claims_nothing_when_the_revisions_match(tmp_path):
    retriever = Indexed("bbb222", working="bbb222")
    outcome = await _code_search(
        _call({"pattern": "total > limit", "output_mode": "content"}),
        _context(tmp_path),
        retriever,
    )

    assert outcome.meta["staleRepos"] == []
    assert "stale index" not in outcome.content
