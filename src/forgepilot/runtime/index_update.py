"""A commit turned into an :class:`IndexUpdate` — the ingestion half of retrieval.

The index is only useful if it learns about commits, and the cheapest correct way
to do that is to ask git what changed rather than to re-read the tree. Three rules
make the delta safe:

* **A rename is two statements**, not one: the old path must be deleted and the
  new one added. Sending only the add leaves the old path answering forever.
* **A delete is sent as a delete**, never as an empty document: an empty file and
  a removed file look the same to a searcher, and only one of them is true.
* **A file that cannot be read textually is dropped and named**, not silently
  skipped — a binary file in the delta is a fact about the repository the job
  should print, not a gap it should hide.

``base..head`` rather than ``base...head``: the index serves a linear history of
file contents, and a three-dot diff would describe changes that are on neither
side.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from forgepilot.seams.index_service import IndexDocument, IndexUpdate
from forgepilot.seams.refs import RepoRef

__all__ = [
    "MAX_DOCUMENT_BYTES",
    "Change",
    "GitRefused",
    "Plan",
    "documents_for",
    "git_delta",
    "git_head",
    "plan",
    "plan_full",
    "tracked_files",
]

#: One file larger than this is not worth a whole index row, and sending it would
#: make a delta into a bulk transfer. Counted in bytes of UTF-8 text.
MAX_DOCUMENT_BYTES = 512 * 1024


class GitRefused(RuntimeError):
    """git could not answer. Distinct from "nothing changed"."""


@dataclass(frozen=True, slots=True)
class Change:
    """One line of ``git diff --name-status``."""

    kind: str
    path: str
    old_path: str = ""

    @property
    def is_delete(self) -> bool:
        return self.kind.startswith("D")

    @property
    def is_rename(self) -> bool:
        return self.kind.startswith("R") or self.kind.startswith("C")


@dataclass(frozen=True, slots=True)
class Plan:
    """An update, plus what could not be put in it.

    ``skipped`` is part of the answer, not a log line: an ingestion job that
    silently drops a binary file produces an index that confidently says "nothing
    there" about code that exists, which is the failure the seam is about.
    """

    update: IndexUpdate
    skipped: tuple[str, ...] = ()
    changes: int = 0


def _assert_checkout_root(root: Path) -> None:
    """Refuse a directory that only *contains* a repository.

    ``git -C <dir> rev-parse HEAD`` walks up to the enclosing repository, so a
    plain directory inside a checkout answers with a commit it does not hold.
    Measured three times now (the index builder, the SWE-bench spike, here), which
    is why the check is not left to the caller.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError as exc:
        raise GitRefused(f"cannot run git in {root}: {exc}") from None
    if done.returncode != 0:
        raise GitRefused(f"{root} is not a git checkout: {done.stderr.strip()}")
    top = Path(done.stdout.strip())
    if top.resolve() != Path(root).resolve():
        raise GitRefused(
            f"{root} is inside the repository at {top}; a delta read there would "
            "describe commits this directory does not hold"
        )


def _git(root: Path, *args: str) -> str:
    _assert_checkout_root(root)
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise GitRefused(f"cannot run git in {root}: {exc}") from None
    if done.returncode != 0:
        raise GitRefused(
            f"git {' '.join(args)} failed in {root}: {done.stderr.strip() or done.stdout.strip()}"
        )
    return done.stdout


def git_head(root: Path) -> str:
    """The commit to index, or a refusal — never a guess."""
    return _git(root, "rev-parse", "HEAD").strip()


def git_delta(root: Path, base: str, head: str) -> tuple[Change, ...]:
    """What changed between two revisions, in the shape a delta needs."""
    raw = _git(
        root,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--find-copies",
        f"{base}..{head}",
    )
    parts = [part for part in raw.split("\0") if part]
    changes: list[Change] = []
    position = 0
    while position < len(parts):
        kind = parts[position]
        position += 1
        if position >= len(parts):
            raise GitRefused(f"git diff --name-status ended after a status with no path: {kind!r}")
        if kind.startswith("R") or kind.startswith("C"):
            if position + 1 >= len(parts):
                raise GitRefused(f"git diff reported {kind} without both paths")
            old, new = parts[position], parts[position + 1]
            position += 2
            changes.append(Change(kind=kind, path=new, old_path=old))
            continue
        changes.append(Change(kind=kind, path=parts[position]))
        position += 1
    return tuple(changes)


def tracked_files(root: Path) -> tuple[str, ...]:
    """Every path git would call content — for the first snapshot of a repository."""
    raw = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return tuple(path for path in raw.split("\0") if path)


def _read_document(root: Path, path: str, max_bytes: int) -> tuple[IndexDocument | None, str]:
    """Read one file as text, or say why it is not a document."""
    full = root / path
    try:
        size = full.stat().st_size
    except OSError as exc:
        return None, f"{path} ({type(exc).__name__})"
    if size > max_bytes:
        return None, f"{path} ({size} bytes, over {max_bytes})"
    try:
        text = full.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None, f"{path} (not UTF-8 text)"
    return IndexDocument(path=path, text=text), ""


def documents_for(
    root: Path, paths: Iterable[str], *, max_bytes: int = MAX_DOCUMENT_BYTES
) -> tuple[list[IndexDocument], tuple[str, ...]]:
    """Read ``paths`` into documents, and return what could not be read."""
    documents: list[IndexDocument] = []
    skipped: list[str] = []
    for path in paths:
        document, refusal = _read_document(root, path, max_bytes)
        if document is None:
            skipped.append(refusal)
        else:
            documents.append(document)
    return documents, tuple(skipped)


def plan(
    root: Path,
    *,
    repo: RepoRef,
    base: str,
    head: str,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> Plan:
    """The delta from ``base`` to ``head``, as one :class:`IndexUpdate`."""
    changes = git_delta(root, base, head)
    deletes: list[str] = []
    wanted: list[str] = []
    for change in changes:
        if change.is_delete:
            deletes.append(change.path)
            continue
        if change.is_rename and change.old_path:
            deletes.append(change.old_path)
        wanted.append(change.path)
    documents, skipped = documents_for(root, wanted, max_bytes=max_bytes)
    update = IndexUpdate(
        repo=repo,
        ref=head,
        documents=tuple(documents),
        deletes=tuple(deletes),
        complete=False,
    )
    return Plan(update=update, skipped=skipped, changes=len(changes))


def plan_full(
    root: Path,
    *,
    repo: RepoRef,
    ref: str | None = None,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> Plan:
    """A complete snapshot — what a job sends when the index has no revision at all."""
    head = ref or git_head(root)
    documents, skipped = documents_for(root, tracked_files(root), max_bytes=max_bytes)
    update = IndexUpdate(repo=repo, ref=head, documents=tuple(documents), complete=True)
    return Plan(update=update, skipped=skipped, changes=len(documents))
