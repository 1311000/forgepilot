"""`--backup` and `--restore` from the command line.

The module tests prove the archive is right; these prove the two flag names a
person types are wired to it, and that the exit code says which happened.
"""

from __future__ import annotations

import json
from pathlib import Path

from forgepilot.cli.main import main


def a_state(root: Path, name: str = "state") -> Path:
    state = root / name
    state.mkdir(parents=True)
    line = {"seq": 1, "kind": "session/start", "data": {"title": "one"}}
    (state / "session-a.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    return state


def test_a_backup_then_a_restore_round_trips(tmp_path, monkeypatch, capsys):
    state = a_state(tmp_path)
    monkeypatch.setenv("FORGEPILOT_HOME", str(state))
    archive = tmp_path / "saved.tar.gz"

    assert main(["--backup", str(archive)]) == 0
    assert archive.is_file()
    assert "backed up" in capsys.readouterr().out

    restored = tmp_path / "restored"
    monkeypatch.setenv("FORGEPILOT_HOME", str(restored))
    assert main(["--restore", str(archive)]) == 0
    output = capsys.readouterr().out
    assert "restored 1 file(s)" in output
    assert (restored / "session-a.jsonl").read_text(encoding="utf-8") == (
        state / "session-a.jsonl"
    ).read_text(encoding="utf-8")


def test_restoring_over_a_live_directory_needs_the_flag(tmp_path, monkeypatch, capsys):
    state = a_state(tmp_path)
    monkeypatch.setenv("FORGEPILOT_HOME", str(state))
    archive = tmp_path / "saved.tar.gz"
    assert main(["--backup", str(archive)]) == 0
    capsys.readouterr()

    # The same directory already holds the state the backup was taken from.
    assert main(["--restore", str(archive)]) == 2
    assert "already holds" in capsys.readouterr().err
    assert main(["--restore", str(archive), "--overwrite"]) == 0
    assert "restored 1 file(s)" in capsys.readouterr().out


def test_the_two_commands_are_not_flags_that_compose(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEPILOT_HOME", str(a_state(tmp_path)))
    try:
        main(["--backup", str(tmp_path / "a.tar.gz"), "--restore", str(tmp_path / "b.tar.gz")])
    except SystemExit as exc:
        assert exc.code != 0
    else:  # pragma: no cover - the parser is supposed to refuse this
        raise AssertionError("--backup and --restore were accepted together")
