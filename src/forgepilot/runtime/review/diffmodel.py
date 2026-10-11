"""Parsing a unified diff, and deciding what in it deserves attention.

Two jobs, both deterministic — no model is involved anywhere in this module, and
that is deliberate. "This hunk is a lockfile" and "these two files are the same
file renamed" are claims a reader can check and a test can pin. Asking a model to
classify them would make the review's most load-bearing judgements unverifiable.

Noise classification is the difference between a review that is short because
there is little to say and one that is short because the tool hid things. So:
**noise is folded, counted and still present** — never dropped. A changed
lockfile is itself a signal (the dependencies moved), and a reader who wants to
look can.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, replace

from forgepilot.seams.review import ChangedHunk, DiffLine, DiffLineKind, NoiseKind

__all__ = ["ParsedFile", "ParsedDiff", "parse_diff", "classify_noise", "detect_renames"]

_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<section>.*)$"
)

#: Paths whose contents are not worth a reviewer's attention by default.
_LOCKFILE_SUFFIXES = (
    "poetry.lock",
    "pipfile.lock",
    "uv.lock",
    "cargo.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "composer.lock",
    "gemfile.lock",
    "go.sum",
)
_GENERATED_MARKERS = (
    "_pb2.py",
    ".pb.go",
    "_generated.",
    ".generated.",
    ".g.dart",
    ".designer.cs",
)
_NOISE_DIRECTORIES = ("node_modules/", "vendor/", "third_party/", "dist/", "build/")

_COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", "<!--", "'")


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """One file's section of the diff."""

    path: str
    hunks: tuple[ChangedHunk, ...] = ()
    old_path: str | None = None
    #: ``/dev/null`` on one side means the file was created or deleted.
    is_new: bool = False
    is_deleted: bool = False
    noise: NoiseKind | None = None
    renamed_from: str | None = None
    binary: bool = False

    @property
    def added(self) -> int:
        return sum(len(h.added) for h in self.hunks)

    @property
    def removed(self) -> int:
        return sum(len(h.removed) for h in self.hunks)

    @property
    def churn(self) -> int:
        return self.added + self.removed


@dataclass(frozen=True, slots=True)
class ParsedDiff:
    """The whole diff, plus the totals a reader wants up front."""

    files: tuple[ParsedFile, ...] = ()
    raw: str = ""

    @property
    def hunks(self) -> tuple[ChangedHunk, ...]:
        return tuple(h for f in self.files for h in f.hunks)

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)

    def hunk_by_id(self, hunk_id: str) -> ChangedHunk | None:
        for hunk in self.hunks:
            if hunk.hunk_id == hunk_id:
                return hunk
        return None


def _strip_label(label: str) -> tuple[str, bool]:
    """``a/src/x.py`` -> ``src/x.py``; ``/dev/null`` -> ``('', True)``."""
    text = label.strip()
    if text in ("/dev/null", ""):
        return "", True
    if text.startswith(("a/", "b/")):
        text = text[2:]
    return text, False


def parse_diff(text: str) -> ParsedDiff:
    """Parse a unified diff into files and hunks.

    Tolerant of the shapes `difflib` emits, including ``/dev/null`` on either
    side for creations and deletions. A malformed section is skipped rather than
    raising: a review that refuses to run because one hunk was odd is worse than
    one that says which part it could not read.
    """
    files: list[ParsedFile] = []

    path = ""
    old_path: str | None = None
    is_new = is_deleted = binary = False
    hunks: list[ChangedHunk] = []
    current: dict | None = None

    def flush_hunk() -> None:
        nonlocal current
        if current is not None:
            hunks.append(
                ChangedHunk(
                    path=path,
                    old_start=current["old_start"],
                    old_count=current["old_count"],
                    new_start=current["new_start"],
                    new_count=current["new_count"],
                    lines=tuple(current["lines"]),
                    section=current["section"],
                )
            )
            current = None

    def flush_file() -> None:
        nonlocal path, old_path, is_new, is_deleted, binary, hunks
        if path or hunks:
            # Creation and deletion are inferred from the hunk headers as well as
            # from `/dev/null`, because **difflib never emits `/dev/null`** — only
            # git does. Keying solely on it meant rename detection silently never
            # fired for diffs this project generates itself, which is every diff
            # the task runner produces.
            #
            #   creation: @@ -0,0 +1,N @@   (nothing on the old side)
            #   deletion: @@ -1,N +0,0 @@   (nothing on the new side)
            new_from_headers = any(h.old_start == 0 and h.old_count == 0 for h in hunks)
            deleted_from_headers = any(h.new_start == 0 and h.new_count == 0 for h in hunks)

            files.append(
                ParsedFile(
                    path=path or (old_path or ""),
                    hunks=tuple(hunks),
                    old_path=old_path,
                    is_new=is_new or new_from_headers,
                    is_deleted=is_deleted or deleted_from_headers,
                    binary=binary,
                )
            )
        path, old_path, is_new, is_deleted, binary, hunks = "", None, False, False, False, []

    for line in text.splitlines():
        if line.startswith("--- "):
            flush_hunk()
            flush_file()
            old_path, old_null = _strip_label(line[4:])
            is_deleted = old_null
            continue

        if line.startswith("+++ "):
            new_path, new_null = _strip_label(line[4:])
            path = new_path or (old_path or "")
            is_new = new_null
            continue

        if line.startswith("Binary files"):
            binary = True
            continue

        match = _HUNK_HEADER.match(line)
        if match:
            flush_hunk()
            current = {
                "old_start": int(match.group("old_start")),
                "old_count": int(match.group("old_count") or 1),
                "new_start": int(match.group("new_start")),
                "new_count": int(match.group("new_count") or 1),
                "section": (match.group("section") or "").strip(),
                "lines": [],
            }
            continue

        if current is None:
            continue

        if line.startswith("+"):
            current["lines"].append(DiffLine(DiffLineKind.ADD, line[1:]))
        elif line.startswith("-"):
            current["lines"].append(DiffLine(DiffLineKind.REMOVE, line[1:]))
        elif line.startswith(" ") or line == "":
            current["lines"].append(DiffLine(DiffLineKind.CONTEXT, line[1:] if line else ""))
        # "\ No newline at end of file" and anything else is ignored.

    flush_hunk()
    flush_file()
    return ParsedDiff(files=tuple(files), raw=text)


# ---------------------------------------------------------------------------
# Noise
# ---------------------------------------------------------------------------
def _is_comment(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and stripped.startswith(_COMMENT_PREFIXES)


def _normalise(lines: tuple[str, ...]) -> list[str]:
    """Drop whitespace, blanks and comment-only lines.

    What is left is what could change behaviour. Comparing the normalised forms
    of the removed and added lines is a crude way to ask "did anything actually
    change", and crude is the right level: it errs toward calling something
    cosmetic only when the tokens are *identical*, so a false "cosmetic" would
    require the code to be literally unchanged.
    """
    out: list[str] = []
    for line in lines:
        if not line.strip() or _is_comment(line):
            continue
        out.append(re.sub(r"\s+", "", line))
    return out


def _path_noise(path: str) -> NoiseKind | None:
    lowered = path.lower()
    if any(lowered.endswith(name) for name in _LOCKFILE_SUFFIXES):
        return NoiseKind.LOCKFILE
    if any(marker in lowered for marker in _GENERATED_MARKERS):
        return NoiseKind.GENERATED
    if ".min." in lowered:
        return NoiseKind.MINIFIED
    if any(lowered.startswith(d) or f"/{d}" in lowered for d in _NOISE_DIRECTORIES):
        return NoiseKind.VENDORED
    return None


def classify_noise(diff: ParsedDiff) -> ParsedDiff:
    """Mark hunks and files that should not consume attention by default."""
    files: list[ParsedFile] = []

    for parsed in diff.files:
        file_noise = _path_noise(parsed.path)
        hunks = []
        for hunk in parsed.hunks:
            if parsed.binary:
                hunks.append(replace(hunk, noise=NoiseKind.BINARY))
            elif file_noise is not None:
                hunks.append(replace(hunk, noise=file_noise))
            elif _normalise(hunk.removed) == _normalise(hunk.added):
                # Same tokens in, same tokens out: whitespace or comments only.
                hunks.append(replace(hunk, noise=NoiseKind.COSMETIC))
            else:
                hunks.append(hunk)

        files.append(
            ParsedFile(
                path=parsed.path,
                hunks=tuple(hunks),
                old_path=parsed.old_path,
                is_new=parsed.is_new,
                is_deleted=parsed.is_deleted,
                noise=file_noise,
                binary=parsed.binary,
            )
        )

    return ParsedDiff(files=tuple(files), raw=diff.raw)


# ---------------------------------------------------------------------------
# Renames
# ---------------------------------------------------------------------------
#: Above this similarity, a deleted file and an added file are treated as one
#: rename rather than two changes. High on purpose: a false pairing would hide a
#: real deletion, while a missed pairing only costs the reader one extra entry.
_RENAME_SIMILARITY = 0.8


def _content_of(file: ParsedFile) -> list[str]:
    return [line.text for hunk in file.hunks for line in hunk.lines]


def detect_renames(diff: ParsedDiff) -> ParsedDiff:
    """Pair deletions with additions that are the same file moved.

    Without this a rename reads as two unrelated changes — a wall of deletions in
    one place and a wall of additions in another — which is exactly the noise
    that makes a reviewer skim. This is the cheap, content-similarity version;
    an AST backend pairs symbols instead, and reports which it used.
    """
    deletions = [f for f in diff.files if f.is_deleted and not f.binary]
    additions = [f for f in diff.files if f.is_new and not f.binary]
    if not deletions or not additions:
        return diff

    pairs: dict[str, str] = {}
    consumed: set[str] = set()

    for gone in deletions:
        gone_content = _content_of(gone)
        if len(gone_content) < 2:
            continue
        best: tuple[float, ParsedFile] | None = None
        for fresh in additions:
            if fresh.path in consumed:
                continue
            ratio = difflib.SequenceMatcher(
                None, "\n".join(gone_content), "\n".join(_content_of(fresh))
            ).ratio()
            if ratio >= _RENAME_SIMILARITY and (best is None or ratio > best[0]):
                best = (ratio, fresh)
        if best is not None:
            consumed.add(best[1].path)
            pairs[best[1].path] = gone.path

    if not pairs:
        return diff

    files: list[ParsedFile] = []
    for parsed in diff.files:
        # Drop only the *deletion* half; the addition is the file that survives,
        # carrying `renamed_from`. Skipping the addition too would delete the
        # rename entirely — which is what an earlier version of this loop did.
        if parsed.path in pairs.values():
            continue
        source = pairs.get(parsed.path)
        if source is None:
            files.append(parsed)
            continue
        files.append(
            ParsedFile(
                path=parsed.path,
                hunks=tuple(replace(h, noise=h.noise or NoiseKind.RENAME) for h in parsed.hunks),
                old_path=source,
                is_new=parsed.is_new,
                is_deleted=parsed.is_deleted,
                noise=parsed.noise or NoiseKind.RENAME,
                renamed_from=source,
            )
        )

    return ParsedDiff(files=tuple(files), raw=diff.raw)
