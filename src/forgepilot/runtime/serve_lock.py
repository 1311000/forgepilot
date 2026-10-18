"""One serving process per workspace, enforced by the filesystem.

The API's session registry is in-process state, so two replicas over one
workspace each hold half the sessions and can stream only their own — a split
brain that looks like a working deployment. The answer is a real deployment:
a shared registry and a message bus. Until that exists, the honest move is to
make the second process *fail to start* instead of quietly dividing the work.

An advisory lock is the smallest mechanism that survives being wrong about
process names: it is released by the kernel when the holder exits, so a killed
container does not leave a lock behind that a human has to find and delete. That
is the difference between this and a pid file, and it is the whole reason to use
one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["AlreadyServing", "hold", "lock_path"]


class AlreadyServing(RuntimeError):
    """Another process holds the lock for this directory."""


def lock_path(directory: Path) -> Path:
    """The lock file inside ``directory``.

    Named rather than anonymous so an operator can see *which* path is contended
    when two deployments collide, and so ``ls`` answers the question without
    strace.
    """
    return Path(directory) / "serve.lock"


def _lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        # Byte-range locks start at the current position, and Windows refuses a
        # zero-length region — one byte is the smallest region that exists.
        os.lseek(fd, _LOCK_OFFSET, 0)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


#: Where the locked byte lives: far past anything the file will hold. A
#: byte-range lock denies reads of the bytes it covers, so locking byte zero
#: would make the pid unreadable by exactly the process that needs to report it.
#: Locking a region beyond EOF is allowed on both platforms, and truncating the
#: file back below it does not release the lock.
_LOCK_OFFSET = 4096


def _holder(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return "an unknown process"
    pid = raw.decode("utf-8", "replace").strip()
    return f"pid {pid}" if pid else "an unknown process"


@contextmanager
def hold(directory: Path) -> Iterator[Path]:
    """Hold the serve lock for ``directory`` for the duration of the block.

    Raises :class:`AlreadyServing` if someone else has it. The message names the
    path and the recorded pid, because "already running" without either is a
    message that sends people looking in the wrong place.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = lock_path(directory)

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            _lock(fd)
        except OSError as exc:
            raise AlreadyServing(
                f"another ForgePilot is already serving this workspace: the lock "
                f"{path} is held by {_holder(path)}. One process owns the session "
                f"registry; run two replicas only once the registry is shared."
            ) from exc

        # The pid is for humans reading the file, not for the lock: correctness
        # comes from the kernel, and a stale pid here is a note, not a state.
        # It lives at the start of the file, well clear of the locked region.
        os.lseek(fd, 0, 0)
        payload = f"{os.getpid()}\n".encode()
        os.write(fd, payload)
        os.truncate(fd, len(payload))

        try:
            yield path
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
