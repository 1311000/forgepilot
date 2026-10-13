"""``FORGEPILOT_TMP`` pins the process temp root, and is *proved* before it does.

Two claims are pinned here, and the second is the one that took a defect to learn:

1. The variable is one knob rather than one provider's knob. Before M15 only
   ``LocalTempWorkspacePlugin`` read it, while ``providers/sandbox/namespace.py``
   called ``tempfile.gettempdir()`` directly — the same setting named two different
   directories depending on who asked.
2. **The directory is proved with a real ``mkstemp``, not assumed from ``mkdir``.**
   On this development box ``%TEMP%``, ``C:\\Windows\\Temp`` and ``c:\\tmp`` all
   exist and all raise ``PermissionError`` on ``mkstemp``, which is exactly why the
   check is written as "can a temp file be created here" rather than "is this a
   directory". A pin that accepted a directory it cannot write to would move the
   failure into whichever unrelated temp call ran next.

Every test passes ``environ=`` explicitly, so none of them depends on the ambient
variable — which on this machine is set at the user level, and on the runner is not.
Assertions are written against the value in force when the test started rather than
against ``None``, because ``tests/conftest.py`` pins a temp root for the whole suite
and fixture ordering decides which of the two runs first.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from forgepilot.env import TempRootUnusable, pin_temp_root


@pytest.fixture(autouse=True)
def restore_tempdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put back whatever this test pins. ``pin_temp_root`` writes the global directly."""
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)


# ---------------------------------------------------------------------------
# Unset: nothing changes
# ---------------------------------------------------------------------------
def test_an_unset_variable_leaves_tempfile_alone() -> None:
    """The unconfigured machine behaves exactly as it did before M15."""
    before = tempfile.tempdir

    assert pin_temp_root(environ={}) is before
    assert tempfile.tempdir == before


def test_an_empty_variable_is_the_same_as_unset() -> None:
    """``FORGEPILOT_TMP=`` is how a shell unsets a variable, and must not be a path."""
    before = tempfile.tempdir

    assert pin_temp_root(environ={"FORGEPILOT_TMP": ""}) is before
    assert tempfile.tempdir == before


# ---------------------------------------------------------------------------
# Set and usable: pinned, proved, and restorable
# ---------------------------------------------------------------------------
def test_the_variable_becomes_the_process_temp_dir(tmp_path: Path) -> None:
    """``tempfile`` really uses it afterwards — that is the whole claim."""
    before = tempfile.tempdir
    target = tmp_path / "temp-root"

    previous = pin_temp_root(environ={"FORGEPILOT_TMP": str(target)})

    assert previous is before
    assert tempfile.tempdir == str(target.resolve())
    handle, name = tempfile.mkstemp()
    os.close(handle)
    os.unlink(name)
    assert Path(name).parent == target.resolve(), "the pin is what tempfile used"


def test_it_returns_the_previous_value_so_a_caller_can_put_it_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``main()`` restores it in a ``finally``; this is the value it restores."""
    earlier = tmp_path / "earlier"
    monkeypatch.setattr(tempfile, "tempdir", str(earlier))
    now = tmp_path / "now"

    previous = pin_temp_root(environ={"FORGEPILOT_TMP": str(now)})

    assert previous == str(earlier)
    assert tempfile.tempdir == str(now.resolve())


def test_a_directory_that_does_not_exist_yet_is_created(tmp_path: Path) -> None:
    """A fresh machine should not have to pre-create it."""
    target = tmp_path / "not" / "there" / "yet"

    pin_temp_root(environ={"FORGEPILOT_TMP": str(target)})

    assert target.is_dir()


# ---------------------------------------------------------------------------
# Set and unusable: refused, named, and nothing pinned
# ---------------------------------------------------------------------------
def test_a_path_that_cannot_hold_a_file_is_refused(tmp_path: Path) -> None:
    """A file in the way — the case where ``mkdir`` and ``mkstemp`` disagree.

    This is the cheap, host-independent stand-in for the box's real accident: the
    parent exists, ``mkdir`` of a child underneath a *file* fails, and the message
    has to name the variable, the resolved path and the underlying reason.
    """
    before = tempfile.tempdir
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(TempRootUnusable) as caught:
        pin_temp_root(environ={"FORGEPILOT_TMP": str(blocker / "child")})

    message = str(caught.value)
    assert "FORGEPILOT_TMP" in message
    assert "cannot hold a temporary file" in message
    assert "child" in message
    assert tempfile.tempdir == before, "a refused root must not be half-pinned"


def test_the_probe_decides_not_the_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The claim in the docstring, made deterministic: ``mkstemp`` is what judges.

    The directory here is perfectly writable, so ``mkdir`` succeeds — and the
    variable is still refused, because the probe is stubbed to fail. That is the
    difference between "this is a directory" and "a temp file can be created here",
    which is the whole reason the check is written the second way.
    """
    before = tempfile.tempdir

    def refuse(*args: object, **kwargs: object) -> tuple[int, str]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", refuse)

    with pytest.raises(TempRootUnusable, match="PermissionError"):
        pin_temp_root(environ={"FORGEPILOT_TMP": str(tmp_path / "writable")})

    assert (tmp_path / "writable").is_dir(), "mkdir succeeded; the probe is what refused"
    assert tempfile.tempdir == before
