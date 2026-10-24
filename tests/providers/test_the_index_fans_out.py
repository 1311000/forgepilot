"""Shards, the fan-out, and the rule that a failed shard is not an empty answer.

In-process tests, because the interesting parts are decisions rather than transport: which
shards a query touches, what a slow shard does to the answer, and how a client must read
a partial one. The socket version lives in `test_the_index_is_sharded.py`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

FORGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE / "tools"))

pytest.importorskip("fastapi", reason="the reference index needs the `web` extra")

import serve_index  # noqa: E402

from forgepilot.providers.retrieve.index import IndexCorpus, IndexedFile  # noqa: E402
from forgepilot.providers.retrieve.remote import HttpIndexService  # noqa: E402
from forgepilot.seams.index_service import (  # noqa: E402
    IndexQuery,
    IndexUnavailable,
)
from forgepilot.seams.refs import RepoRef  # noqa: E402
from forgepilot.seams.security import ExecutionConstraints  # noqa: E402

MONO = RepoRef("git.corp", "team", "mono")
TINY = RepoRef("git.corp", "team", "tiny")


def _corpus() -> IndexCorpus:
    files = [
        IndexedFile(MONO, "web/app.py", "main", "def app():\n    return 'web'\n"),
        IndexedFile(MONO, "web/view.py", "main", "def view():\n    return 'view'\n"),
        IndexedFile(MONO, "api/handlers.py", "main", "def handle():\n    return 'api'\n"),
        IndexedFile(MONO, "README.md", "main", "# mono\n"),
        IndexedFile(TINY, "one.py", "main", "def one():\n    return 1\n"),
    ]
    return IndexCorpus(files, ())


def test_a_top_directory_layout_splits_a_repository_and_keeps_root_files_in_place() -> None:
    shards = serve_index.shards_of(_corpus(), by="top-dir")
    names = [shard.name for shard in shards]
    assert names == [
        "git.corp/team/mono",
        "git.corp/team/mono#api",
        "git.corp/team/mono#web",
        "git.corp/team/tiny",
    ]
    by_name = {shard.name: shard for shard in shards}
    assert by_name["git.corp/team/mono"].prefix == ""
    assert by_name["git.corp/team/mono#web"].prefix == "web/"
    assert len(by_name["git.corp/team/mono#web"].corpus.files) == 2


def test_a_repository_layout_is_one_shard_per_repository() -> None:
    shards = serve_index.shards_of(_corpus(), by="repo")
    assert [shard.name for shard in shards] == ["git.corp/team/mono", "git.corp/team/tiny"]
    assert all(shard.prefix == "" for shard in shards)


def test_an_empty_repository_list_selects_no_shard_rather_than_every_shard() -> None:
    shards = serve_index.shards_of(_corpus(), by="repo")
    chosen, skipped = serve_index.choose_shards(shards, ())
    assert chosen == ()
    assert len(skipped) == 2


def test_a_glob_selects_only_the_shards_that_could_contain_it() -> None:
    shards = serve_index.shards_of(_corpus(), by="top-dir")
    chosen, skipped = serve_index.choose_shards(shards, (MONO,), ("web/**",))
    assert [shard.name for shard in chosen] == ["git.corp/team/mono#web"]
    assert "git.corp/team/mono#api" in [shard.name for shard in skipped]


def test_a_glob_with_no_directory_still_selects_the_root_shard() -> None:
    shards = serve_index.shards_of(_corpus(), by="top-dir")
    chosen, _skipped = serve_index.choose_shards(shards, (MONO,), ("README.md",))
    assert "git.corp/team/mono" in [shard.name for shard in chosen]


def test_a_slow_shard_is_reported_by_name_and_the_others_still_answer() -> None:
    shards = serve_index.shards_of(_corpus(), by="top-dir")

    async def ask(corpus: Any, shard: Any) -> list[str]:
        if shard.name.endswith("#web"):
            await asyncio.sleep(0.5)
        return [shard.name]

    answers, failed, answered = asyncio.run(serve_index.fan_out(shards, ask, deadline_s=0.15))

    assert failed and failed[0]["shard"] == "git.corp/team/mono#web"
    assert "no answer within" in failed[0]["reason"]
    assert set(answered) == {shard.name for shard in shards} - {"git.corp/team/mono#web"}
    assert len(answers) == len(answered)


def test_a_shard_that_raises_is_reported_with_its_exception() -> None:
    shards = serve_index.shards_of(_corpus(), by="repo")

    async def ask(corpus: Any, shard: Any) -> list[str]:
        raise RuntimeError(f"{shard.name} is broken")

    _answers, failed, answered = asyncio.run(serve_index.fan_out(shards, ask, deadline_s=1.0))

    assert answered == ()
    assert {entry["shard"] for entry in failed} == {shard.name for shard in shards}
    assert "is broken" in failed[0]["reason"]


def test_the_deadline_is_clamped_in_both_directions() -> None:
    class _State:
        deadline_s = 1.2

    assert serve_index._deadline({"deadlineMs": 0}, _State()) == pytest.approx(0.001)
    assert serve_index._deadline({"deadlineMs": 10**9}, _State()) == pytest.approx(30.0)
    assert serve_index._deadline({}, _State()) == pytest.approx(1.2)
    assert serve_index._deadline({"deadlineMs": "nonsense"}, _State()) == pytest.approx(1.2)


async def test_a_client_refuses_a_partial_answer_instead_of_returning_the_rest() -> None:
    body = {
        "shards": {
            "answered": ["git.corp/team/mono#api"],
            "failed": [{"shard": "git.corp/team/mono#web", "reason": "no answer within 0.15s"}],
            "skipped": [],
        },
        "hits": [],
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    service = HttpIndexService(
        "http://index.test", token="t", transport=httpx.MockTransport(handler)
    )
    constraints = ExecutionConstraints(
        principal_id="p", tenant_id="demo", allowed_repos=frozenset({MONO})
    )
    with pytest.raises(IndexUnavailable) as caught:
        await service.search(IndexQuery(pattern="app", repos=(MONO,), ref="main"), constraints)
    assert "mono#web" in str(caught.value)
    assert "not an empty result" in str(caught.value)
    assert len(seen) == 1


async def test_a_client_returns_hits_when_every_shard_answered() -> None:
    body = {
        "shards": {"answered": ["git.corp/team/mono"], "failed": [], "skipped": []},
        "hits": [],
    }
    service = HttpIndexService(
        "http://index.test",
        token="t",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    constraints = ExecutionConstraints(
        principal_id="p", tenant_id="demo", allowed_repos=frozenset({MONO})
    )
    assert (
        await service.search(IndexQuery(pattern="app", repos=(MONO,), ref="main"), constraints)
        == []
    )
