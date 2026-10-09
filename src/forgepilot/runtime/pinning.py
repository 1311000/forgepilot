"""Running a project's checks against the project's *own* tests.

The hole this closes: an agent that cannot make a check pass can make the check
stop testing the thing. ``run_checks`` passes either way, and nothing in the
harness could tell "fixed the code" from "weakened the assertion" — a green
result means one thing if the oracle was held fixed and another if the thing
being measured was also being edited.

So the oracle is held fixed. Every changed path the project counts as a test is
restored to the revision the session started from, the checks run against
*that*, and the agent's versions are put back afterwards so the diff still shows
what it wrote.

Why this and not "flag test changes for review": a flag relies on somebody
reading it, and the failure it guards against is precisely the one that looks
fine. Restoring is a mechanism — it makes the weakened test *fail*, which is a
signal the repair loop already knows how to act on.

Two things it deliberately does not do:

- **It does not stop the agent editing tests.** Adding a regression test is
  ordinary good practice, and forbidding it would be the wrong trade. The edit
  is kept, shown in the diff, and simply does not decide the run.
- **It does not pin paths the project does not call tests.** A file that is
  genuinely part of the change has to stay the agent's, or every run would be
  judged against code that does not contain the fix.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

__all__ = ["pinned_to_base"]

#: Distinguishes "the agent's file was empty" from "there was no file". ``None``
#: and ``b""`` are both legal file contents.
_MISSING = object()


def _read(root: Path, path: str) -> Any:
    target = root / path
    try:
        return target.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return _MISSING


def _write(root: Path, path: str, content: Any) -> None:
    target = root / path
    if content is _MISSING:
        # A file the agent created. Deleting it is the other half of pinning:
        # `restore` removed it for the check, and leaving it gone would silently
        # drop the agent's work from the diff.
        if target.is_file():
            target.unlink()
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


@contextlib.asynccontextmanager
async def pinned_to_base(
    workspace: Any, handle: Any, paths: Sequence[str]
) -> AsyncIterator[tuple[str, ...]]:
    """Yield the paths pinned, with them matching the handle's base revision.

    Yields the paths it actually pinned rather than ``paths``, because the two are
    the same only by accident: this is what the caller will report to a reviewer,
    and a report that names a pin which did not happen is worse than no report.

    Restoring is best-effort on the way *out* and never raises. If putting the
    agent's file back fails, the run is already over and an exception here would
    replace a real check result with a filesystem error — the check is the thing
    being reported.
    """
    if not paths:
        yield ()
        return

    root: Path = handle.root
    saved = {path: _read(root, path) for path in paths}

    await workspace.restore(handle, handle.base_ref, paths)
    try:
        yield tuple(paths)
    finally:
        for path, content in saved.items():
            with contextlib.suppress(OSError):
                _write(root, path, content)


def unpinned_notice(pinned: Sequence[str]) -> str:
    """What to tell the model about the pinning, or ``''`` when nothing is pinned.

    It has to say *editing the tests will not help*. Without that the repair loop
    reads a failure, does the obvious thing — adjusts the assertion — and fails
    again, because its edit was never in the tree the check ran against. A loop
    with no way to learn why it is failing is a loop that burns its budget.
    """
    if not pinned:
        return ""
    listed = ", ".join(f"`{path}`" for path in pinned)
    return (
        f"[the project's tests were restored to the revision this task started from "
        f"for this run: {listed}. The checks are being judged against those, not "
        f"against any change to them, so editing a test cannot make this pass — fix "
        f"the code, or say why the test itself is wrong.]"
    )
