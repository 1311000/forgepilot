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

__all__ = ["candidate_files", "load_env_file", "load_local_env"]

#: Loaded in order; later files do not override earlier ones or the environment.
FILENAMES = (".env.local",)


def load_env_file(path: Path) -> list[str]:
    """Load ``KEY=value`` pairs from ``path``. Returns the names it set.

    Only names are returned — callers must not need the values, and a return type
    that carried them would invite printing them.
    """
    if not path.is_file():
        return []

    applied: list[str] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
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
