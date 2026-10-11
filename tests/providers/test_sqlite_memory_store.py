"""The durable memory store: what changes because the store is a file.

The seam's rules are the same for both stores and are tested once, in
``tests/contract/test_memory_model.py`` — which runs every case against *every*
provider of the seam, so "which store am I talking to" cannot change the answer to
"may this be believed". What is left for this file is everything that is only true
*because* the store is a file:

* a memory outlives the process that wrote it, including one that is genuinely a
  second process rather than a second object;
* loading a profile does not create the file — the same rule the JSONL watermark
  store keeps (``providers/distillation/watermark.py:48-50``);
* several tenants and several repositories can share one file without seeing each
  other, which is the shape a deployment actually has;
* the same claim distilled twice is one row rather than two;
* a bundle can point the ``memory`` seam at this store without touching code.

The cross-process case is the one the milestone rests on: with a single
interpreter, an in-process dict passes every other test in this file too.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from forgepilot.config import apply_layers, resolve_plugin
from forgepilot.config.bundle import load_bundle_layer
from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.seams import (
    EventRef,
    ExecutionConstraints,
    MemoryCandidate,
    MemoryScope,
    MemoryStatus,
    MemoryStore,
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

REPO_ROOT = Path(__file__).resolve().parents[2]
SQLITE_SPEC = "forgepilot.providers.memory.sqlite:SqliteMemoryStorePlugin"

TENANT = TenantId("acme")
OTHER_TENANT = TenantId("other")
REPO = RepoRef(host="git.corp", namespace="team", name="widget")
OTHER_REPO = RepoRef(host="git.corp", namespace="team", name="gadget")
SESSION = SessionId("s-durable")

TEXT = "the widget build always runs make check"


def ev(seq: int = 3) -> EventRef:
    return EventRef(session_id=SESSION, seq=seq)


def constraints(
    tenant: TenantId = TENANT, allowed: tuple[RepoRef, ...] = (REPO,)
) -> ExecutionConstraints:
    principal = Principal(principal_id=PrincipalId("alice"), tenant_id=tenant)
    return ExecutionConstraints.for_principal(
        principal,
        allowed_repos=frozenset(allowed or (REPO,)),
        permitted_operations=frozenset({Operation.READ_CODE, Operation.RUN_TESTS}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


def candidate(**overrides: object) -> MemoryCandidate:
    base: dict[str, object] = {
        "text": TEXT,
        "scope": MemoryScope.REPO,
        "scope_ref": REPO.slug,
        "type": MemoryType.COMMAND,
        "subject": "widget build",
        "tenant_id": TENANT,
    }
    base.update(overrides)
    return MemoryCandidate(**base)  # type: ignore[arg-type]


def observed(*events: EventRef, repo: RepoRef = REPO) -> Provenance:
    """A human said it, so it is recallable and the harness watched them say it.

    ``repo`` is the repository the harness read the evidence *in*, which narrows a
    recall on top of the record's own scope — so a test about another repository has
    to move both, or it is describing a memory learned here and filed there.
    """
    return Provenance(
        kind=ProvenanceKind.HUMAN_VERIFIED,
        session_id=SESSION,
        repo=repo,
        evidence_refs=events or (ev(),),
    )


def guessed() -> Provenance:
    """A model's guess: no evidence, which is exactly what a proposal is."""
    return Provenance(kind=ProvenanceKind.MODEL_ASSERTED, session_id=SESSION, repo=REPO)


async def recall(
    store: MemoryStore,
    *,
    text: str = "",
    tenant: TenantId = TENANT,
    allowed: tuple[RepoRef, ...] = (REPO,),
    scope_ref: str = REPO.slug,
) -> tuple[object, ...]:
    query = RecallQuery(text=text, scope=MemoryScope.REPO, scope_ref=scope_ref)
    return tuple(await store.recall(query, constraints(tenant, allowed)))


@asynccontextmanager
async def store_at(path: Path) -> AsyncIterator[MemoryStore]:
    """A root whose ``memory`` seam is a SQLite store at ``path``, disposed after."""
    root = create_root()
    fiber = await root.plugin(resolve_plugin(SQLITE_SPEC), {"path": str(path)})
    try:
        yield root.memory
    finally:
        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# it is a file
# ---------------------------------------------------------------------------
async def test_a_memory_outlives_the_store_that_wrote_it(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"

    async with store_at(path) as first:
        await first.write(candidate(), observed())
        assert len(first.records) == 1

    async with store_at(path) as second:
        hits = await recall(second)
        assert [hit.text for hit in hits] == [TEXT]
        assert hits[0].provenance.kind is ProvenanceKind.HUMAN_VERIFIED
        # The recall that just happened is part of the record, and it was counted
        # by the second store rather than read out of the first one's memory.
        assert hits[0].recall_count == 1


async def test_loading_the_store_does_not_create_the_file(tmp_path: Path) -> None:
    """Booting a profile must not have filesystem side effects.

    ``--boot`` loads every enabled row, and a row that writes on construction turns
    "show me the resolved tree" into a mutation — which is why the file (and its
    directory) appears on the first ``write``, not on the first connection.
    """
    path = tmp_path / "nested" / "memory.sqlite3"

    async with store_at(path) as store:
        assert not path.exists()
        assert not path.parent.exists()

        await store.write(candidate(), observed())

        assert path.exists()
        assert path.parent.exists()


async def test_two_tenants_share_one_file_without_seeing_each_other(
    tmp_path: Path,
) -> None:
    """The same sentence, in two tenants, is two memories and neither leaks.

    The text is deliberately identical: if the tenant were not part of the record's
    identity, the second write would de-duplicate onto the first and one tenant
    would silently read the other's memory.
    """
    async with store_at(tmp_path / "memory.sqlite3") as store:
        mine = await store.write(candidate(), observed())
        theirs = await store.write(candidate(tenant_id=OTHER_TENANT), observed())

        assert mine != theirs
        assert len(store.records) == 2
        assert [hit.tenant_id for hit in await recall(store)] == [TENANT]
        assert [hit.tenant_id for hit in await recall(store, tenant=OTHER_TENANT)] == [
            OTHER_TENANT
        ]


async def test_a_memory_learned_in_another_repository_is_not_served_here(
    tmp_path: Path,
) -> None:
    """Two repositories, one file — and the audience check comes before any read."""
    async with store_at(tmp_path / "memory.sqlite3") as store:
        await store.write(
            candidate(scope_ref=OTHER_REPO.slug, subject="widget release"),
            observed(repo=OTHER_REPO),
        )

        # Asking about this repository gets nothing: the memory is scoped to the other.
        assert await recall(store) == ()
        # A caller who may read the other repository does get it, and only then.
        assert [
            hit.text
            for hit in await recall(
                store, scope_ref=OTHER_REPO.slug, allowed=(OTHER_REPO,)
            )
        ] == [TEXT]
        # Asking for it by name is not the same as being allowed to read it.
        assert (
            await recall(store, scope_ref=OTHER_REPO.slug, allowed=(REPO,))
        ) == ()


async def test_the_same_claim_written_twice_in_two_processes_is_one_row(
    tmp_path: Path,
) -> None:
    """De-duplication is by content hash, so it cannot depend on an in-process counter.

    This is also the reason the store derives an id from the hash rather than
    counting: two processes distilling the same session would both hand out
    ``mem-1``, and the second insert would lose to the primary key.
    """
    path = tmp_path / "memory.sqlite3"

    async with store_at(path) as first:
        written = await first.write(candidate(), observed())

    async with store_at(path) as second:
        again = await second.write(candidate(), observed())

        assert again == written
        assert len(second.records) == 1


async def test_a_revocation_survives_the_process(tmp_path: Path) -> None:
    """Forgetting has to be durable too, or a restart resurrects what was revoked."""
    path = tmp_path / "memory.sqlite3"

    async with store_at(path) as first:
        memory_id = await first.write(candidate(), observed())
        await first.revoke(memory_id)

    async with store_at(path) as second:
        assert await recall(second) == ()
        assert [record.status for record in second.records] == [MemoryStatus.REVOKED]


async def test_an_expired_proposal_is_retired_rather_than_left_standing(
    tmp_path: Path,
) -> None:
    """A proposal with a deadline keeps it across a reopen.

    ``PROPOSED`` is a recallable status, so the deadline is what retires a guess
    nobody ever endorsed — and it is checked before the provenance kind, which
    means the difference is observable: expired, or silently still a proposal.
    """
    past = datetime.now(UTC) - timedelta(days=1)
    path = tmp_path / "memory.sqlite3"

    async with store_at(path) as first:
        await first.admit_provisional(candidate(expires_at=past), guessed())
        assert [record.status for record in first.records] == [MemoryStatus.PROPOSED]

    async with store_at(path) as second:
        assert await recall(second) == ()
        assert [record.status for record in second.records] == [MemoryStatus.EXPIRED]


# ---------------------------------------------------------------------------
# two processes, one file
# ---------------------------------------------------------------------------
PROCESS_ONE = '''"""Process 1: distil one session into the durable store, then exit.

Run by ``test_a_second_process_recalls_what_the_first_process_distilled`` in its own
interpreter. It shares nothing with the test process except the two files it is
given, which is the whole point: "memory across sessions" is otherwise a claim made
inside one interpreter, where an in-process dict would pass just as well.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime

from forgepilot.config import resolve_plugin
from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.runtime.distillation import distill
from forgepilot.runtime.session import Session
from forgepilot.seams.refs import RepoRef, SessionId, TurnId
from forgepilot.seams.session import SessionHeader

MEMORY_PATH, WATERMARK_PATH = sys.argv[1], sys.argv[2]
TENANT = "acme"
REPO = RepoRef(host="git.corp", namespace="team", name="widget")
SESSION = SessionId("s-other-process")
UTTERANCE = "the widget build always runs make check"


async def main() -> None:
    root = create_root()
    fibers = []
    for spec, config in (
        ("forgepilot.providers.session_store.memory:InMemorySessionStorePlugin", {}),
        ("forgepilot.providers.memory.sqlite:SqliteMemoryStorePlugin", {"path": MEMORY_PATH}),
        ("forgepilot.providers.llm.replay:ReplayLLMPlugin", {"model": "replay-1"}),
        (
            "forgepilot.providers.distillation.watermark:JsonlWatermarkPlugin",
            {"path": WATERMARK_PATH},
        ),
    ):
        fibers.append(await root.plugin(resolve_plugin(spec), config))

    await root.sessionPersistence.create(
        SessionHeader(
            session_id=SESSION,
            created_at=datetime.now(UTC),
            tenant=TENANT,
            principal="alice",
        )
    )
    session = await Session.open(root.sessionPersistence, SESSION)
    await session.turn_start(TurnId(1))
    said = await session.user_message(TurnId(1), UTTERANCE)
    await session.turn_end(TurnId(1), "completed")
    await session.flush()

    # The model answers with the candidate JSON the distiller's schema demands, and
    # cites the event that carries the sentence — so the harness reads the trust off
    # the log instead of believing the model about it.
    root.llm.script(
        json.dumps(
            [
                {
                    "type": "command",
                    "subject": "widget build",
                    "text": UTTERANCE,
                    "evidence_seqs": [said.seq],
                    "model_confidence_hint": 0.9,
                }
            ]
        )
    )
    report = await distill(
        session,
        llm=root.llm,
        memory=root.memory,
        watermarks=root.distillationWatermarks,
        scope_ref=REPO.slug,
        tenant_id=TENANT,
        repo=REPO,
        model="replay-1",
    )
    print(f"written={report.written} provisional={report.provisional} failed={report.failed}")
    for fiber in reversed(fibers):
        await dispose_fiber(fiber)


asyncio.run(main())
'''


async def test_a_second_process_recalls_what_the_first_process_distilled(
    tmp_path: Path,
) -> None:
    """Distil in one process, recall in another — with only the files in common."""
    script = tmp_path / "distil_elsewhere.py"
    script.write_text(PROCESS_ONE, encoding="utf-8")
    memory_path = tmp_path / "memory.sqlite3"
    watermark_path = tmp_path / "watermarks.jsonl"

    completed = subprocess.run(
        [sys.executable, str(script), str(memory_path), str(watermark_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "written=1" in completed.stdout, completed.stdout
    assert memory_path.exists()

    async with store_at(memory_path) as store:
        assert len(store.records) == 1
        hits = await recall(store, text="make check")

    assert [hit.text for hit in hits] == [TEXT]
    assert hits[0].tenant_id == TENANT
    assert hits[0].provenance.kind is ProvenanceKind.HUMAN_VERIFIED


# ---------------------------------------------------------------------------
# the deployment path
# ---------------------------------------------------------------------------
def test_the_bundle_points_the_memory_row_at_the_durable_store() -> None:
    """``--patch src/forgepilot/bundles/memory_sqlite/patch.yaml`` replaces the row.

    A bundle nothing loads is the defect shape with a directory around it, so this
    asserts on the resolved tree: the row's plugin *and* its config, plus the sources
    that show which layer won.
    """
    resolved = apply_layers([load_bundle_layer("base"), load_bundle_layer("memory_sqlite")])
    rows = {row.id: row for row in resolved.rows}
    memory = rows["memory"]

    assert memory.plugin == SQLITE_SPEC
    assert dict(memory.config) == {"path": ""}
    assert memory.sources == ("bundle:base", "bundle:memory_sqlite")
    assert memory.tier == "trusted"
