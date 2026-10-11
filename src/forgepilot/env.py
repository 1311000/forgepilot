"""Local environment overrides.

ForgePilot reads every secret from the environment — never from a config file,
because config files are committed and printed by ``--dump-config``. That leaves
the everyday problem that exporting six variables before every shell is tedious,
which is what this module is for: a small ``.env.local`` file that is
**gitignored**, loaded at start-up.

Deliberate properties:

- **The real environment always wins.** A variable already set is never
  overridden, so ``export X=...`` in a shell still beats the file. Surprising
  precedence here would make debugging miserable.
- **Values are never logged.** Not the names' values, not on error. A loader that
  echoes what it found is a loader that leaks.
- **A missing file is fine.** Nothing here is required; absent files simply mean
  the variables must come from somewhere else.
"""

from __future__ import annotations

import os
from pathlib import Path

from forgepilot.errors import ForgePilotError

__all__ = ["EnvFileUnreadable", "candidate_files", "load_env_file", "load_local_env"]

#: Loaded in order; later files do not override earlier ones or the environment.
FILENAMES = (".env.local",)


class EnvFileUnreadable(ForgePilotError):
    """The env file is not UTF-8 text, so it cannot be read as an env file.

    The alternative is what used to happen: the NUL-laced bytes were read as a
    *name*, and `os.environ["F\\x00O\\x00R\\x00…"] = …` raised
    `ValueError: embedded null character` — from inside `main()`, before any
    command has run, naming neither the file nor the encoding.
    """


def load_env_file(path: Path) -> list[str]:
    """Load ``KEY=value`` pairs from ``path``. Returns the names it set.

    Only names are returned — callers must not need the values, and a return type
    that carried them would invite printing them.
    """
    if not path.is_file():
        return []

    raw_bytes = path.read_bytes()
    _require_utf8_text(path, raw_bytes)

    applied: list[str] = []
    # Not `enumerate`: the line number is not used. The loader names the *file* when
    # it refuses one, and `_require_utf8_text` names the problem; a counter that
    # nothing reads used to sit here and suggested otherwise. (Found by `ruff`'s B007.)
    for raw in raw_bytes.decode("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            # Ignored rather than fatal: a stray line in a personal dotfile should
            # not stop the program, and there is nothing to report that would not
            # risk echoing file contents.
            continue

        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if not name:
            continue

        if name in os.environ:
            continue  # the real environment wins
        os.environ[name] = value
        applied.append(name)

    return applied


def _require_utf8_text(path: Path, raw: bytes) -> None:
    """Refuse a file that is not UTF-8, and say which file and why.

    PowerShell's `>>` writes **UTF-16**, so appending one credential to an
    otherwise UTF-8 `.env.local` leaves a mixed-encoding file. The bytes are still
    "valid UTF-8" — NUL bytes are — so the old reader split on `=` quite happily
    and handed `os.environ` a name of `F\\x00O\\x00R\\x00G\\x00…`.

    What that produced was `ValueError: embedded null character`, raised out of
    `main()` before any command ran, mentioning neither the file nor the
    encoding. Measured, on a file of exactly this shape — the first version of
    this docstring said the line was *silently ignored*, which is a different bug
    and was not the one.

    A NUL byte is the tell, and checking for it costs one pass over a few hundred
    bytes. It cannot appear in a UTF-8 env file anybody meant to write.
    """
    if b"\x00" in raw:
        raise EnvFileUnreadable(
            f"{path} contains NUL bytes, so it is not UTF-8 text — most likely "
            f"UTF-16, which is what PowerShell's `>>` writes by default. Appending "
            f"one line that way to a UTF-8 file silently produces names like "
            f"'F\\x00O\\x00R\\x00…' that no one will ever read.\n"
            f"Fix: rewrite the file as UTF-8, or append with "
            f"`Add-Content -Encoding utf8 <value>` from PowerShell. In Git Bash, "
            f"`echo 'NAME=value' >> .env.local` is already correct."
        )

    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvFileUnreadable(
            f"{path} is not valid UTF-8 ({exc.reason} at byte {exc.start}). "
            f"Rewrite it as UTF-8 — a file this loader cannot read is a file whose "
            f"variables are silently missing."
        ) from exc


def candidate_files(start: Path | None = None) -> tuple[Path, ...]:
    """Where a local env file may live, most specific first."""
    here = (start or Path.cwd()).resolve()
    home = os.environ.get("FORGEPILOT_HOME")

    places: list[Path] = []
    for directory in (here, *here.parents):
        places.extend(directory / name for name in FILENAMES)
    if home:
        places.append(Path(home) / ".env")

    seen: set[Path] = set()
    unique: list[Path] = []
    for place in places:
        if place not in seen:
            seen.add(place)
            unique.append(place)
    return tuple(unique)


def load_local_env(start: Path | None = None) -> list[str]:
    """Load the first env file found. Returns the names that were set."""
    for candidate in candidate_files(start):
        if candidate.is_file():
            return load_env_file(candidate)
    return []
