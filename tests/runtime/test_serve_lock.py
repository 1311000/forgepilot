"""One server per state directory, and the lock must not outlive its holder."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from forgepilot.runtime.serve_lock import AlreadyServing, hold, lock_path


def test_a_second_process_cannot_take_the_same_lock(tmp_path: Path) -> None:
    """Two handles on one file, which is what two processes look like to the OS."""
    with hold(tmp_path):
        with pytest.raises(AlreadyServing) as caught:
            with hold(tmp_path):
                pass

    assert str(lock_path(tmp_path)) in str(caught.value)
    assert "session registry" in str(caught.value)


def test_the_lock_file_records_the_pid_and_is_reusable_after_release(
    tmp_path: Path,
) -> None:
    with hold(tmp_path) as path:
        recorded = path.read_bytes().replace(b"\x00", b"").decode().strip()
        assert recorded == str(os.getpid())

    # Released: the *next* process (here, the next block) takes it cleanly. This
    # is the property a pid file does not have — nothing here has to know whether
    # the previous holder exited or crashed.
    with hold(tmp_path) as path:
        assert path == lock_path(tmp_path)


def test_the_directory_is_created_if_it_does_not_exist(tmp_path: Path) -> None:
    """A fresh deployment has no state directory yet."""
    nested = tmp_path / "state" / "serve"

    with hold(nested):
        assert (nested / "serve.lock").is_file()


def test_the_holder_is_named_in_the_refusal(tmp_path: Path) -> None:
    """ "Already running" without a pid is a message that sends people guessing."""
    with hold(tmp_path):
        with pytest.raises(AlreadyServing) as caught:
            with hold(tmp_path):
                pass

    assert f"pid {os.getpid()}" in str(caught.value)
