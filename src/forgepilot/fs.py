"""Filesystem operations that are not one-liners on every platform.

One function, and it exists because **git marks its own object files read-only**
and Windows ``shutil.rmtree`` raises ``PermissionError`` on them instead of
clearing the attribute. Deleting a checkout therefore means changing a mode bit
and retrying, and the failure mode without it is a ``PermissionError`` naming a
path inside ``.git/objects`` — which reads as "something is using this file"
rather than as "this file is read-only".

It lives here rather than beside its first caller because there are three:
recreating the example fixture, recreating the seed checkout, and replacing a
mirror whose layout has changed. The first two are scripts under ``examples/``
and the third is the repo-host provider, so the shared part is not the *policy*
of when to delete — each of those decides for itself, deliberately — but the
mechanism of how.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

__all__ = ["remove_tree"]


def remove_tree(path: Path) -> None:
    """Remove a directory tree, including files marked read-only.

    The ``onexc`` handler is not a blanket "force it": an error that is not
    ``PermissionError`` is re-raised untouched, because a directory that cannot
    be removed for some *other* reason is a real failure and swallowing it would
    turn it into a confusing "the clone silently did not happen" later.
    """

    def handle(func, target, exc):  # type: ignore[no-untyped-def]
        if not isinstance(exc, PermissionError):
            raise exc
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=handle)
