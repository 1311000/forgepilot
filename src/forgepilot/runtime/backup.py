"""Backing up and restoring the one state directory a server owns.

**Why this exists at all.** Everything durable lives under
``$FORGEPILOT_HOME``: the session logs (append-only JSONL), the memory database,
the distillation watermark. The serve lock makes one process the owner of that
directory, which is the property a backup needs and the reason for its main
restriction: **it refuses to run while a server holds the lock.** A copy taken
while another process appends can catch a half-written line; ``recover_tail``
would happily repair that on the next boot, which is exactly the problem — the
backup would look fine and be subtly different from the log it claims to be.

**A manifest, not a bare tarball.** Every archived file carries its size and
SHA-256, and restoring verifies both before anything is moved into place. A
tarball that was truncated by a full disk extracts without complaint; a manifest
turns that into a refusal that names the file.

**Validation is by content, not by extension.** Each ``*.jsonl`` is parsed line by
line, because "it restores and then the server cannot read it" is the failure this
whole module exists to prevent, and a checksum proves the bytes were copied, not
that they are a log.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from forgepilot.runtime.serve_lock import AlreadyServing, hold

__all__ = [
    "FORMAT",
    "MANIFEST",
    "BackupRefused",
    "backup",
    "restore",
    "read_manifest",
]

FORMAT = 1
MANIFEST = "manifest.json"

#: Never archived: the lock is this process's own claim, and a restored lock file
#: would tell the next server that somebody else owns the directory.
_EXCLUDED = frozenset({"serve.lock"})


class BackupRefused(RuntimeError):
    """A backup or restore that would produce something untrue."""


def _files(state_dir: Path) -> list[Path]:
    if not state_dir.is_dir():
        raise BackupRefused(f"{state_dir} is not a directory, so there is nothing to back up")
    found = [
        path
        for path in sorted(state_dir.rglob("*"))
        if path.is_file() and path.name not in _EXCLUDED
    ]
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lines(path: Path) -> Iterable[tuple[int, str]]:
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for number, line in enumerate(handle, start=1):
            if line.strip():
                yield number, line


def _check_readable(path: Path) -> None:
    """Parse the logs, so "it restored" is not the only claim being made."""
    if path.suffix != ".jsonl":
        return
    for number, line in _lines(path):
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            raise BackupRefused(
                f"{path} line {number} is not JSON ({exc}); this is a log with a "
                "half-written record, and copying it would preserve the damage"
            ) from exc


def backup(state_dir: Path, destination: Path) -> Path:
    """Write one archive of ``state_dir`` and return its path.

    Refuses while another process serves the directory — see the module
    docstring for why a torn line is worse than a refused backup.
    """
    state_dir = Path(state_dir)
    try:
        with hold(state_dir):
            files = _files(state_dir)
            if not files:
                raise BackupRefused(f"{state_dir} has no state to back up")
            for path in files:
                _check_readable(path)
            entries = [
                {
                    "path": path.relative_to(state_dir).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                }
                for path in files
            ]
            manifest = {
                "format": FORMAT,
                "created_at": datetime.now(UTC).isoformat(),
                "source": str(state_dir),
                "files": entries,
            }
    except AlreadyServing as exc:
        raise BackupRefused(
            f"{exc} A backup taken while it appends can catch a half-written line, so "
            "stop the server (or point FORGEPILOT_HOME elsewhere) and take it again."
        ) from exc

    destination = Path(destination)
    # `Path("x.tar.gz").suffix` is ".gz", not ".tar.gz" — asking it whether this is
    # an archive path answers no for every filename a person would actually type,
    # and the first smoke test of this module was a backup written into a directory
    # called `saved.tar.gz`.
    if destination.is_dir() or not destination.name.endswith(".tar.gz"):
        destination = destination / f"forgepilot-state-{datetime.now(UTC):%Y%m%dT%H%M%S}.tar.gz"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST)
        info.size = len(payload)
        info.mtime = int(datetime.now(UTC).timestamp())
        archive.addfile(info, io.BytesIO(payload))
        for path in files:
            archive.add(path, arcname=path.relative_to(state_dir).as_posix(), recursive=False)
    return destination


def read_manifest(archive: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        try:
            member = tar.getmember(MANIFEST)
        except KeyError as exc:
            raise BackupRefused(
                f"{archive} has no {MANIFEST}; it is not a ForgePilot backup, or it was "
                "written by a version that did not record what it copied"
            ) from exc
        handle = tar.extractfile(member)
        if handle is None:
            raise BackupRefused(f"{archive}: {MANIFEST} is not a regular file")
        manifest = json.loads(handle.read().decode("utf-8"))
    if manifest.get("format") != FORMAT:
        raise BackupRefused(
            f"{archive} is format {manifest.get('format')!r}; this build reads {FORMAT}"
        )
    return manifest


def _extract(archive: Path, into: Path) -> dict:
    manifest = read_manifest(archive)
    with tarfile.open(archive, "r:gz") as tar:
        # Refuse any member that would land outside the target: an archive is
        # input, and an archive is the one thing here that came from elsewhere.
        for member in tar.getmembers():
            if member.name == MANIFEST:
                continue
            target = (into / member.name).resolve()
            if not str(target).startswith(str(into.resolve())):
                raise BackupRefused(f"{archive} contains {member.name!r}, which escapes the target")
        tar.extractall(into, filter="data")  # noqa: S202 - members checked above
    for entry in manifest.get("files", []):
        path = into / entry["path"]
        if not path.is_file():
            raise BackupRefused(f"{archive} claims {entry['path']} but does not contain it")
        if path.stat().st_size != entry["size"]:
            raise BackupRefused(
                f"{archive}: {entry['path']} is {path.stat().st_size} bytes, the manifest "
                f"says {entry['size']}"
            )
        if _sha256(path) != entry["sha256"]:
            raise BackupRefused(f"{archive}: {entry['path']} does not match its checksum")
        _check_readable(path)
    return manifest


def restore(archive: Path, state_dir: Path, *, overwrite: bool = False) -> dict:
    """Replace ``state_dir``'s contents with the archive's, verifying first.

    Everything is extracted and checked in a **temporary directory beside the
    target**, so a refusal leaves the running directory untouched; only a fully
    verified archive is moved into place.
    """
    archive, state_dir = Path(archive), Path(state_dir)
    if not archive.is_file():
        raise BackupRefused(f"{archive} does not exist")
    try:
        with hold(state_dir):
            # The lock file is this process's own claim, created by the line above.
            # Counting it as "state that would be replaced" made a second restore
            # into a directory restored once refuse for a reason that is not about
            # the archive at all.
            existing = (
                [path for path in _files(state_dir) if path.name not in _EXCLUDED]
                if state_dir.is_dir()
                else []
            )
            if existing and not overwrite:
                raise BackupRefused(
                    f"{state_dir} already holds {len(existing)} file(s); restoring would "
                    "replace them. Pass overwrite after taking a backup of the current "
                    "state, or restore into an empty directory."
                )
            state_dir.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=state_dir.parent))
            try:
                manifest = _extract(archive, staging)
                for path in existing:
                    path.unlink()
                for path in sorted(staging.rglob("*")):
                    if path.is_file():
                        relative = path.relative_to(staging)
                        if relative.as_posix() == MANIFEST:
                            # The manifest describes the archive; it is not state.
                            continue
                        target = state_dir / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(path), str(target))
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    except AlreadyServing as exc:
        raise BackupRefused(
            f"{exc} Restoring while a server owns the directory would replace files "
            "it has open, so stop it first."
        ) from exc
    return manifest
