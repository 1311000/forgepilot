"""The ``.env.local`` loader.

This module had no tests until it produced a bug somebody had to debug for a
credential appended from **PowerShell**, whose `>>` writes UTF-16, on a file the
loader reads as UTF-8.

The consequence was not subtle, and it was not the one first written down here.
NUL bytes are valid UTF-8, so the line was read as a *name* of
``F\\x00O\\x00R\\x00G\\x00…`` and handed to `os.environ`, which raised
``ValueError: embedded null character`` — out of `main()`, before any command
ran, naming neither the file nor the encoding. Measured, not assumed: the first
version of this docstring described a silent skip, which is a different bug.

So these tests are mostly about **what the loader refuses and how it says so**.
An error that names the file and the cause is worth more than one that names
neither, and both are worth more than a variable that quietly is not there.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch):
    """Nothing here may leak into ``os.environ`` or depend on what is already there."""
    for name in ("ALPHA", "BETA", "GAMMA", "FORGEPILOT_GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield


def write(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / ".env.local"
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# It reads ordinary files
# ---------------------------------------------------------------------------
def test_it_loads_key_value_pairs_and_returns_the_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from forgepilot.env import load_env_file

    path = write(tmp_path, b"ALPHA=1\n\n# a comment\nBETA=two\n")

    names = load_env_file(path)

    assert names == ["ALPHA", "BETA"]
    assert os.environ["ALPHA"] == "1"
    assert os.environ["BETA"] == "two"


def test_it_strips_export_and_quotes(tmp_path: Path) -> None:
    """A file shared with a shell script should not need rewriting to be read.

    A quoted value is also what somebody gets from copying a shell snippet, and
    a token wrapped in quotes fails authentication for a reason that looks like
    "the token is wrong".
    """
    import os

    from forgepilot.env import load_env_file

    path = write(tmp_path, b'export GAMMA="quoted value"\n')

    load_env_file(path)

    assert os.environ["GAMMA"] == "quoted value"


def test_the_real_environment_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit `export` in a shell must beat the file, or debugging is misery."""
    import os

    from forgepilot.env import load_env_file

    monkeypatch.setenv("ALPHA", "from-the-shell")
    path = write(tmp_path, b"ALPHA=from-the-file\n")

    names = load_env_file(path)

    assert os.environ["ALPHA"] == "from-the-shell"
    assert names == [], "an overridden name was reported as applied"


def test_a_missing_file_is_fine(tmp_path: Path) -> None:
    from forgepilot.env import load_env_file

    assert load_env_file(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# What it refuses, and why refusing is the point
# ---------------------------------------------------------------------------
def test_utf16_raises_with_a_message_that_names_the_file_and_the_cause(
    tmp_path: Path,
) -> None:
    """The bug. The old failure was `ValueError: embedded null character`.

    PowerShell's `>>` writes UTF-16, so appending one credential to an otherwise
    UTF-8 file leaves a mixed-encoding file. NUL bytes are valid UTF-8, so the
    old reader split on `=` quite happily and handed `os.environ` a name of
    `F\\x00O\\x00R\\x00G\\x00…` — which raised, from inside `main()`, before any
    command ran, naming neither the file nor the encoding. The fix is not that it
    now fails; it already failed. The fix is that it now says what is wrong.
    """
    from forgepilot.env import EnvFileUnreadable, load_env_file

    # Exactly the shape: UTF-8 lines, then one appended by PowerShell.
    mixed = b"ALPHA=1\n" + "FORGEPILOT_GITHUB_TOKEN=ghp_x".encode("utf-16-le") + b"\n"

    with pytest.raises(EnvFileUnreadable) as caught:
        load_env_file(write(tmp_path, mixed))

    message = str(caught.value)
    assert "NUL" in message
    assert "PowerShell" in message, "the message does not name the cause"
    assert "UTF-8" in message, "the message does not name the fix"


def test_a_whole_file_in_utf16_raises_too(tmp_path: Path) -> None:
    """The other shape the same mistake produces, when `>` creates the file."""
    from forgepilot.env import EnvFileUnreadable, load_env_file

    with pytest.raises(EnvFileUnreadable):
        load_env_file(write(tmp_path, "ALPHA=1\n".encode("utf-16")))


def test_bytes_that_are_not_utf8_at_all_raise(tmp_path: Path) -> None:
    from forgepilot.env import EnvFileUnreadable, load_env_file

    with pytest.raises(EnvFileUnreadable) as caught:
        load_env_file(write(tmp_path, "ALPHA=caf\xe9\n".encode("latin-1")))

    assert "UTF-8" in str(caught.value)


def test_a_broken_file_fails_loudly_rather_than_being_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load_local_env` must not quietly move on to the next candidate.

    A skip would leave every variable unset, and the symptom would be an error
    about a *credential* — which is where the reader would then spend their time.
    """
    from forgepilot.env import EnvFileUnreadable, load_local_env

    write(tmp_path, "ALPHA=1\n".encode("utf-16"))

    with pytest.raises(EnvFileUnreadable):
        load_local_env(tmp_path)
