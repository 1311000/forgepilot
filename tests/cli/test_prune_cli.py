"""``--prune`` from the command line: the cutoff, the count, and the audit event.

The seam tests prove *what* may be deleted; these prove the flag a person types is
wired to it, that ``--dry-run`` really leaves the rows alone, and that the run
leaves an event behind — the store cannot write one, so "who deleted these rows and
when" has to be answerable from the log.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from forgepilot.cli.main import main

FORGE = Path(__file__).resolve().parents[2]
CONFIGS = str(FORGE / "configs")
BUNDLE = str(FORGE / "src/forgepilot/bundles/memory_sqlite/patch.yaml")
TOKENS = json.dumps([{"token": "demo-token", "principal_id": "alice", "tenant_id": "default"}])


def seed(state: Path) -> None:
    """A store with one revoked record — history no recall could serve."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber
    from forgepilot.seams import (
        EventRef,
        MemoryCandidate,
        MemoryScope,
        MemoryType,
        Provenance,
        ProvenanceKind,
        RepoRef,
        SessionId,
        TenantId,
    )

    spec = "forgepilot.providers.memory.sqlite:SqliteMemoryStorePlugin"
    repo = RepoRef(host="git.corp", namespace="team", name="svc-a")
    session = SessionId("s-seed")

    async def run() -> None:
        root = create_root()
        fiber = await root.plugin(resolve_plugin(spec), {"path": str(state / "memory.sqlite3")})
        try:
            memory_id = await root.memory.write(
                MemoryCandidate(
                    text="the build used make check",
                    scope=MemoryScope.REPO,
                    scope_ref=repo.slug,
                    type=MemoryType.COMMAND,
                    subject="build command",
                    tenant_id=TenantId("default"),
                ),
                Provenance(
                    kind=ProvenanceKind.HUMAN_VERIFIED,
                    session_id=session,
                    repo=repo,
                    evidence_refs=(EventRef(session, 1),),
                ),
            )
            await root.memory.revoke(memory_id)
        finally:
            await dispose_fiber(fiber)

    asyncio.run(run())


def rows(state: Path) -> int:
    with closing(sqlite3.connect(state / "memory.sqlite3")) as conn:
        return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


def prune(state: Path, monkeypatch, *extra: str) -> int:
    monkeypatch.setenv("FORGEPILOT_HOME", str(state))
    monkeypatch.setenv("FORGEPILOT_TMP", str(state / "workspaces"))
    monkeypatch.setenv("FORGEPILOT_DEV_TOKENS", TOKENS)
    return main(
        [
            "--config-dir",
            CONFIGS,
            "--patch",
            BUNDLE,
            "--prune",
            "--token",
            "demo-token",
            *extra,
        ]
    )


def test_prune_deletes_history_and_names_the_count(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir()
    seed(state)
    assert rows(state) == 1

    assert prune(state, monkeypatch, "--older-than-days", "0") == 0
    output = capsys.readouterr().out
    assert "deleted 1 record(s)" in output
    assert "session: memory-maintenance-default" in output
    assert rows(state) == 0


def test_a_dry_run_reports_the_same_number_and_deletes_nothing(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir()
    seed(state)

    assert prune(state, monkeypatch, "--older-than-days", "0", "--dry-run") == 0
    assert "would delete 1 record(s)" in capsys.readouterr().out
    assert rows(state) == 1, "a dry run that deleted rows would be the worst of both"


def test_the_cutoff_is_what_keeps_a_recent_row(tmp_path, monkeypatch, capsys):
    """The default is 90 days, so today's history is kept — that is the whole point."""
    state = tmp_path / "state"
    state.mkdir()
    seed(state)

    assert prune(state, monkeypatch) == 0
    assert "deleted 0 record(s)" in capsys.readouterr().out
    assert rows(state) == 1


def test_the_prune_is_in_the_log(tmp_path, monkeypatch):
    """The store keeps no log, so the event is the only audit trail there is."""
    state = tmp_path / "state"
    state.mkdir()
    seed(state)

    assert prune(state, monkeypatch, "--older-than-days", "0") == 0

    # The JSONL store files a session under `<state>/sessions/<tenant>/<month>/`, so
    # the path is found rather than composed: the layout is the store's business, and
    # pinning it here would make a storage change look like a prune defect.
    (maintenance,) = [
        path for path in (state / "sessions").rglob("memory-maintenance-default.jsonl")
    ]
    kinds = [
        json.loads(line)["kind"]
        for line in maintenance.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "memory/pruned" in kinds
