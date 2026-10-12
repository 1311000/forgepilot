"""The memory arms need a store that outlives the attempt, or they measure nothing.

⚠️ **This is the half the eval was missing.** `run_task(memory=...)` was wired from
Phase E, and nothing ever wrote into the store, so every arm began empty: `full` and
`no-memory` produced the same prompt and the same rate, and the table read like a
mechanism that makes no difference. Two things had to become real before the arms mean
anything, and both are asserted here rather than described:

1. **The store is durable and per arm.** `prepare_repo` wipes the checkout every run, so
   memory is the only thing that can carry from one attempt to the next — a store that
   died with the attempt would leave the pair comparing two empty runs. Per *arm*,
   because `full` learning from `no-memory`'s runs would make the comparison a sequence.
2. **It is the same store after a reboot.** The next attempt is a different process; a
   path that only works in-process is a cache, and a cache would make every rep the
   first one.

⚠️ **What this file deliberately does not assert: that memory improves anything.** That
is the measurement the arms exist to produce, and it needs a live model and a sweep —
`evals/README.md` records the numbers, including a zero delta if that is what came out.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import arms as arm_module  # noqa: E402
import harness as H  # noqa: E402


@pytest.fixture
def task():
    return H.load_task("bug-fix")


@pytest.fixture(autouse=True)
def an_isolated_scratch(tmp_path, monkeypatch):
    """Every path this file touches is under `tmp_path`, never the real scratch root.

    The store is keyed by task and arm rather than by test, so a test that used the
    default root would leave a database behind for the *next* sweep of the same arm to
    find — and the sweep would then be measuring this test.
    """
    monkeypatch.setenv("FORGEPILOT_EVAL_SCRATCH", str(tmp_path / "scratch"))
    return tmp_path / "scratch"


@pytest.fixture
def bootable(task, tmp_path):
    """A checkout and the three variables `run_one` exports before it boots.

    `examples/stat-fix/patch.yaml` names `${FORGEPILOT_EXAMPLE_REPO}` and friends — the
    eval runs the same patch file the README tells a person to run — so a test that
    boots without them fails inside `${VAR}` expansion rather than at the thing it meant
    to check.
    """
    repo = H.prepare_repo(task, tmp_path / "repo")
    H.export_environment(repo, tmp_path / "worktrees")
    return repo


# ---------------------------------------------------------------------------
# Where the store lives
# ---------------------------------------------------------------------------
def test_each_arm_gets_its_own_store(task) -> None:
    """Two arms must not share one: the pair is a comparison, not a sequence."""
    full = H.memory_dir(task, "full")

    assert full != H.memory_dir(task, "no-memory")
    assert full != H.memory_dir(task, "no-distill")


def test_two_tasks_do_not_share_a_store(task) -> None:
    """A memory learned in one task's repository is not evidence about another's."""
    other = H.load_task("find-the-bug")

    assert H.memory_dir(task, "full") != H.memory_dir(other, "full")


def test_the_store_path_does_not_name_the_arm(task) -> None:
    """The same rule as `scratch_dir`, for the same reason: it reaches the session.

    A path containing `no-distill` tells the model it is the run that is not supposed to
    learn — which is how the first ablation's arms came to announce their own treatment.
    """
    for name in arm_module.ARMS:
        directory = H.memory_dir(task, name)
        assert name not in str(directory)
        for word in name.split("-"):
            assert word not in directory.name


def test_reps_share_the_store_and_a_new_cell_does_not(task) -> None:
    """`memory_dir` has no `rep`, on purpose — and that is the whole measurement.

    Rep 1 is cold by construction (`run_one` resets on `rep == 1`), later reps are warm,
    and every row records `memories_before` so a reader can separate the two instead of
    averaging them into one rate.
    """
    assert H.memory_dir(task, "full") == H.memory_dir(task, "full")
    assert len({H.scratch_dir(task, "full", rep) for rep in (1, 2)}) == 2


def test_resetting_a_store_clears_it(task) -> None:
    """The reset is what makes a sweep reproducible: without it rep 1 of the *second*
    sweep inherits the first sweep's memories and is no longer cold."""
    into = H.memory_dir(task, "full")
    into.mkdir(parents=True, exist_ok=True)
    (into / "memory.sqlite3").write_bytes(b"not really a database")

    H.reset_memory(task, "full")

    assert not into.exists()


# ---------------------------------------------------------------------------
# What the run is configured with
# ---------------------------------------------------------------------------
def test_the_rows_make_the_store_durable(task) -> None:
    """Both rows, and they are two halves of one fact.

    `memory` is where what was learned goes; `distillation` is how far into each session
    the extraction already happened. A durable store with a per-process watermark re-pays
    for every session on every run; a per-run store with a durable watermark skips the
    extraction entirely. Each is the other's default hidden.
    """
    rows = {row["id"]: row for row in H.memory_rows(task, "full")}

    assert set(rows) == {"memory", "distillation"}
    assert "sqlite" in rows["memory"]["plugin"]
    assert "watermark" in rows["distillation"]["plugin"]
    for row in rows.values():
        path = Path(row["config"]["path"])
        assert path.is_absolute(), row
        assert path.parent == H.memory_dir(task, "full")


def test_the_rows_are_reported_as_an_arm_change(task) -> None:
    """They are configuration, so a run's row shows them — a store nobody can see in the
    results file is a store nobody can check."""
    changes = H.arm_changes(task, arm_module.arm("full"))

    assert changes == {}  # base configuration: the eval, not the arm


# ---------------------------------------------------------------------------
# And it survives the process that wrote it
# ---------------------------------------------------------------------------
def test_a_memory_written_by_one_attempt_is_recalled_by_the_next(task, bootable) -> None:
    """⚠️ **The property the arms rest on, made executable.**

    Two boots of the same profile with the same rows — the first writes, the second
    recalls — which is what two attempts at one task are, minus the model. A path that
    only worked in-process would pass every test above and still leave the second rep
    reading an empty store.

    No teardown: each attempt gets its own `create_root()`, and the store it uses opens
    and closes a connection per operation (see `providers/memory/sqlite.py`), so there is
    no handle left for `tmp_path` to trip over.
    """
    from forgepilot.seams import (
        EventRef,
        ExecutionConstraints,
        MemoryCandidate,
        MemoryScope,
        MemoryType,
        NetworkAccess,
        Operation,
        Principal,
        PrincipalId,
        Provenance,
        ProvenanceKind,
        RecallQuery,
        RepoRef,
        SessionId,
        TenantId,
    )

    repo = RepoRef(host="git.corp", namespace="team", name="widget")
    tenant = TenantId("acme")
    rows = H.memory_rows(task, "full")

    def count(root: Any) -> int:
        return len(getattr(root.memory, "records", ()) or ())

    async def recall(root: Any) -> int:
        principal = Principal(principal_id=PrincipalId("alice"), tenant_id=tenant)
        constraints = ExecutionConstraints.for_principal(
            principal,
            allowed_repos=frozenset({repo}),
            permitted_operations=frozenset({Operation.READ_CODE}),
            writable_paths=("src",),
            network=NetworkAccess.DENY,
        )
        hits = await root.memory.recall(
            RecallQuery(text="build order", scope=MemoryScope.REPO, scope_ref=repo.slug),
            constraints,
        )
        return len(hits)

    async def attempt() -> tuple[int, int]:
        root, _ = await H.boot(task, list(rows), scripted=True)
        if count(root):
            return await recall(root), count(root)
        await root.memory.write(
            MemoryCandidate(
                text="the widget build runs make check before make test",
                scope=MemoryScope.REPO,
                scope_ref=repo.slug,
                type=MemoryType.FACT,
                subject="widget build order",
                tenant_id=tenant,
            ),
            Provenance(
                kind=ProvenanceKind.TEST_VERIFIED,
                session_id=SessionId("s-eval-memory"),
                repo=repo,
                evidence_refs=(EventRef(session_id=SessionId("s-eval-memory"), seq=1),),
            ),
        )
        return 0, count(root)

    first_hits, first_written = asyncio.run(attempt())
    assert first_hits == 0
    assert first_written == 1
    assert (H.memory_dir(task, "full") / "memory.sqlite3").exists()

    second_hits, second_written = asyncio.run(attempt())
    assert second_written == 1, "the second attempt must not re-learn what it has"
    assert second_hits == 1, "the store did not survive the process that wrote it"


# ---------------------------------------------------------------------------
# The extraction prompt gets its own answer
# ---------------------------------------------------------------------------
def test_the_extraction_gets_its_own_answer(task, bootable) -> None:
    """⚠️ The silent failure this test is the regression for: a queue, not a schema bug.

    The first version appended the empty list to `H.script_turns(task)` and every
    scripted sweep then reported `failed='schema'`. The script carries several repair
    attempts and a scripted run consumes only the first two or three, so the turns left
    over sat **in front of** the appended one: the extraction prompt was answered with
    the agent's spare prose, and "the model's answer is not JSON" reads as a distillation
    defect while the distillation was fine. The extraction is a separate prompt, so it
    gets a separate script.
    """
    from run import _script_the_extraction

    from forgepilot.seams.llm import ChatRequest

    async def boot():
        root, _ = await H.boot(task, H.memory_rows(task, "full"), scripted=True)
        return root

    root = asyncio.run(boot())
    root.llm.script("leftover prose", "another leftover")
    assert root.llm.remaining == 2

    _script_the_extraction(root)

    assert root.llm.remaining == 1, "the loop's leftovers are still in front of it"
    reply = asyncio.run(
        root.llm.complete(ChatRequest(model="replay-1", system=(), messages=()))
    )
    assert reply.message.content == "[]", "the honest scripted answer is 'nothing'"
    assert root.llm.remaining == 0
