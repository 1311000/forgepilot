"""The state directory, copied in a way that can be trusted afterwards.

The value of a backup is not that a file exists; it is that restoring it gives a
directory the server can open. Every test here therefore reads back through the
same validation the restore path uses.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from forgepilot.runtime import backup as backup_module
from forgepilot.runtime.backup import BackupRefused, backup, read_manifest, restore
from forgepilot.runtime.serve_lock import AlreadyServing

LINES = [
    {"seq": 1, "kind": "session/start", "data": {"title": "one"}},
    {"seq": 2, "kind": "turn/start", "data": {"text": "hello"}},
]


def a_state(root: Path) -> Path:
    state = root / "state"
    state.mkdir(parents=True)
    log = state / "session-a.jsonl"
    log.write_text("".join(json.dumps(line) + "\n" for line in LINES), encoding="utf-8")
    (state / "memory.sqlite3").write_bytes(b"SQLite format 3\x00" + b"\x00" * 32)
    nested = state / "distill"
    nested.mkdir()
    (nested / "watermark.json").write_text('{"cursor": 2}', encoding="utf-8")
    return state


def rewrite(archive: Path, *, member: str, payload: bytes) -> Path:
    """Rebuild the archive with one member replaced, manifest left alone."""
    target = archive.with_name("tampered-" + archive.name)
    with tarfile.open(archive, "r:gz") as source, tarfile.open(target, "w:gz") as sink:
        for entry in source.getmembers():
            handle = source.extractfile(entry)
            data = handle.read() if handle is not None else b""
            if entry.name == member:
                data = payload
            info = tarfile.TarInfo(entry.name)
            info.size = len(data)
            sink.addfile(info, io.BytesIO(data))
    return target


def test_a_backup_restores_into_an_empty_directory(tmp_path):
    state = a_state(tmp_path)
    archive = backup(state, tmp_path / "saved.tar.gz")

    assert archive.is_file()
    manifest = read_manifest(archive)
    assert manifest["format"] == 1
    assert {entry["path"] for entry in manifest["files"]} == {
        "session-a.jsonl",
        "memory.sqlite3",
        "distill/watermark.json",
    }

    fresh = tmp_path / "restored"
    restore(archive, fresh)

    for entry in manifest["files"]:
        assert (fresh / entry["path"]).read_bytes() == (state / entry["path"]).read_bytes()


def test_the_manifest_is_not_state_and_a_lock_file_is_not_a_reason_to_refuse(tmp_path):
    state = a_state(tmp_path)
    archive = backup(state, tmp_path / "saved.tar.gz")
    fresh = tmp_path / "restored"
    fresh.mkdir()
    # A directory a server has once run in holds nothing but its own lock file.
    (fresh / "serve.lock").write_text("4321", encoding="utf-8")

    restore(archive, fresh)
    assert not (fresh / "manifest.json").exists()
    assert (fresh / "session-a.jsonl").read_text(encoding="utf-8") == (
        state / "session-a.jsonl"
    ).read_text(encoding="utf-8")


def test_the_lock_file_is_never_archived(tmp_path):
    state = a_state(tmp_path)
    (state / "serve.lock").write_text("1234", encoding="utf-8")

    archive = backup(state, tmp_path / "saved.tar.gz")

    assert "serve.lock" not in {entry["path"] for entry in read_manifest(archive)["files"]}


def test_a_backup_while_a_server_serves_is_refused(tmp_path, monkeypatch):
    state = a_state(tmp_path)

    def held(_directory):
        raise AlreadyServing("the lock is held by pid 4242")

    monkeypatch.setattr(backup_module, "hold", held)
    with pytest.raises(BackupRefused) as caught:
        backup(state, tmp_path / "saved.tar.gz")

    assert "pid 4242" in str(caught.value)
    assert "half-written" in str(caught.value)


def test_a_half_written_log_is_refused_by_name_and_line(tmp_path):
    state = a_state(tmp_path)
    with (state / "session-a.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 3, "kind": "tur')

    with pytest.raises(BackupRefused) as caught:
        backup(state, tmp_path / "saved.tar.gz")

    message = str(caught.value)
    assert "session-a.jsonl line 3" in message
    assert "half-written" in message
    assert not (tmp_path / "saved.tar.gz").exists()


def test_a_tampered_file_is_refused_before_it_replaces_anything(tmp_path):
    state = a_state(tmp_path)
    archive = backup(state, tmp_path / "saved.tar.gz")
    original = (state / "session-a.jsonl").read_bytes()
    # Same length, different bytes: the size check has to pass so that the
    # checksum check is the one that refuses.
    payload = original.replace(b'"one"', b'"two"')
    assert len(payload) == len(original) and payload != original
    tampered = rewrite(archive, member="session-a.jsonl", payload=payload)

    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("do not lose me", encoding="utf-8")

    with pytest.raises(BackupRefused) as caught:
        restore(tampered, target, overwrite=True)

    assert "does not match its checksum" in str(caught.value)
    # The refusal happened in staging, so the live directory is untouched.
    assert (target / "keep.txt").read_text(encoding="utf-8") == "do not lose me"


def test_restoring_over_a_directory_needs_an_explicit_overwrite(tmp_path):
    state = a_state(tmp_path)
    archive = backup(state, tmp_path / "saved.tar.gz")
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.jsonl").write_text('{"seq": 1}\n', encoding="utf-8")

    with pytest.raises(BackupRefused) as caught:
        restore(archive, target)
    assert "already holds" in str(caught.value)
    assert (target / "old.jsonl").is_file()

    restore(archive, target, overwrite=True)
    assert not (target / "old.jsonl").exists()
    assert (target / "session-a.jsonl").read_text(encoding="utf-8") == (
        state / "session-a.jsonl"
    ).read_text(encoding="utf-8")


def test_an_archive_without_a_manifest_is_not_a_backup(tmp_path):
    plain = tmp_path / "plain.tar.gz"
    with tarfile.open(plain, "w:gz") as archive:
        payload = b"hello"
        info = tarfile.TarInfo("session-a.jsonl")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(BackupRefused) as caught:
        read_manifest(plain)
    assert "has no manifest.json" in str(caught.value)
