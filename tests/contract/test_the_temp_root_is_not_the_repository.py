"""Where a test run's temporary files go, decided rather than inherited.

⚠️ ``tempfile`` falls back to the current directory when no candidate is writable, and
on this development host that fallback is what happens: every real candidate
(``%TEMP%``, ``%TMP%``, ``C:\\Windows\\Temp``, ``c:\\tmp``) raises
``PermissionError: [Errno 13] Permission denied`` on ``mkstemp``, and the repository
root is the only creatable one. That is how pytest's ``tmp_path`` trees — thousands of
files — and ForgePilot's own ``forgepilot-*`` workspaces came to sit in the front door
of the project.

``tests/conftest.py`` answers it at import time by pinning the temp root to
``.tmp/tmp``. This test is the reason the pin cannot quietly stop working: it is red on
this box the moment the pin is removed, and green anywhere a real temp directory exists.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

FORGE = Path(__file__).resolve().parents[2]


def test_the_temporary_directory_is_not_the_repository() -> None:
    resolved = Path(tempfile.gettempdir()).resolve()

    assert resolved != FORGE, (
        f"`tempfile.gettempdir()` is the repository itself ({resolved}), so every test "
        "that asks for a temporary directory writes into the project — `pytest`'s "
        "`tmp_path` trees and any `forgepilot-*` workspace among them. See the pin at "
        "the top of `tests/conftest.py`."
    )
