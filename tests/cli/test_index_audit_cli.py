"""`forgepilot --index-audit SESSION_ID` — the reader P27 exists for.

The module tests fold events; these prove the flag a person types is wired to the durable
log and that the exit code says which verdict it reached. `0` means the code that was read is
the code that changed; `3` means "look, it does not" *or* "I cannot tell" — and the second of
those must never be `0`, the same rule `--check` follows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgepilot.cli.main import main
from forgepilot.seams.session import EventKind, SessionEvent

A = "a" * 40
B = "b" * 40
API = "git.corp/team/api"


def a_session(home: Path, session_id: str, events: list[SessionEvent]) -> Path:
    """Write one session in the layout the JSONL store reads, without booting the kernel."""
    directory = home / "sessions" / "default" / "202610"
    directory.mkdir(parents=True, exist_ok=True)
    header = {
        "session_id": session_id,
        "created_at": "2026-10-25T00:00:00+00:00",
        "format_version": 1,
        "tenant": "default",
        "principal": "alice",
        "cwd": str(home),
        "parent_session": None,
        "seed_boundary": None,
    }
    (directory / f"{session_id}.header.json").write_text(json.dumps(header), encoding="utf-8")
    lines = [json.dumps(event.to_json()) for event in events]
    (directory / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


def an_event(seq: int, kind: str, data: dict) -> SessionEvent:
    return SessionEvent(seq=seq, kind=kind, data=data)


def a_read(seq: int, ref: str, *, stale: bool = False) -> SessionEvent:
    meta: dict = {"repo": API, "path": "limits.py", "ref": ref, "start": 1, "end": 9}
    if stale:
        meta["stale"] = True
        meta["workingRef"] = B
    return an_event(seq, EventKind.TOOL_RESULT, {"callId": "c", "meta": meta, "content": "x"})


def a_delivery(seq: int, commit: str) -> SessionEvent:
    return an_event(
        seq, EventKind.TASK_STATE, {"repo": API, "state": "delivered", "commit": commit}
    )


def test_a_consistent_session_exits_zero(tmp_path, monkeypatch, capsys) -> None:
    a_session(tmp_path, "s1", [a_read(1, A), a_delivery(2, A)])
    monkeypatch.setenv("FORGEPILOT_HOME", str(tmp_path))

    assert main(["--config-dir", "configs", "--index-audit", "s1"]) == 0
    output = capsys.readouterr().out
    assert "verdict: consistent" in output
    assert "| git.corp/team/api |" in output


def test_a_mixed_session_exits_three_and_names_the_revisions(tmp_path, monkeypatch, capsys) -> None:
    a_session(tmp_path, "s2", [a_read(1, B), a_delivery(2, A)])
    monkeypatch.setenv("FORGEPILOT_HOME", str(tmp_path))

    assert main(["--config-dir", "configs", "--index-audit", "s2"]) == 3
    output = capsys.readouterr().out
    assert "verdict: mixed" in output
    assert B[:12] in output and A[:12] in output


def test_a_session_with_nothing_to_compare_exits_three_not_zero(
    tmp_path, monkeypatch, capsys
) -> None:
    """No delivery means no verdict — and "I cannot tell" is not a pass."""
    a_session(tmp_path, "s3", [a_read(1, A)])
    monkeypatch.setenv("FORGEPILOT_HOME", str(tmp_path))

    assert main(["--config-dir", "configs", "--index-audit", "s3"]) == 3
    assert "verdict: unknown" in capsys.readouterr().out


def test_an_unknown_session_is_an_error_not_a_verdict(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sessions").mkdir()
    monkeypatch.setenv("FORGEPILOT_HOME", str(tmp_path))

    assert main(["--config-dir", "configs", "--index-audit", "nope"]) == 2
    assert "no session" in capsys.readouterr().err


def test_the_audit_is_one_of_the_exclusive_modes() -> None:
    """It must not be combinable with another command: two actions, one exit code."""
    with pytest.raises(SystemExit):
        main(["--config-dir", "configs", "--index-audit", "s1", "--backup", "x.tar.gz"])
