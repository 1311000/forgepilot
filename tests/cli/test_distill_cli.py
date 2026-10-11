"""``forgepilot --distill``: distillation as a command, not a library call.

Most of what is asserted here is *refusal*, for the same reason as `test_task_cli.py`:
the happy path needs a stored session, a model and a network, so it is covered by
`tests/memory/test_distillation.py`; what a CLI test can hold down is that the
command says no for the right reason and never quietly does the wrong thing.

⚠️ **`FORGEPILOT_TMP` is set in two of these tests, and that is not tidiness.** The
default profile's `workspace` row refuses to fall back to `tempfile`'s last resort
(see `docs/notes/task-delivery.md`), and on a box where every real temp directory is
unwritable — this one — `tempfile.gettempdir()` returns the repository root, so the
row fails and boot stops. The CLI is right to refuse; the point is that the failure
must name the directory it will not use, which is what the first test asserts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgepilot.cli.main import main

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPO_ROOT / "configs"


def test_two_commands_at_once_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--distill", "s-1", "--chat"])

    assert caught.value.code == 2
    assert "separate commands" in capsys.readouterr().err


def test_a_distillation_without_a_repository_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An audience cannot be defaulted.

    A memory's audience is a repository; a memory filed with no audience is one that
    cannot be kept out of another team's recall, so defaulting it would be inventing
    a permission model in a `--repo` fallback.
    """
    code = main(["--config-dir", str(CONFIGS), "--distill", "s-1"])

    assert code == 2
    err = capsys.readouterr().err
    assert "--repo host/namespace/name" in err
    assert "audience" in err


def test_a_half_a_repository_slug_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    """`team/widget` is the same repository name on every host."""
    code = main(["--config-dir", str(CONFIGS), "--distill", "s-1", "--repo", "team/widget"])

    assert code == 2
    assert "--repo host/namespace/name" in capsys.readouterr().err


def test_a_since_that_is_not_a_timestamp_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "--config-dir",
            str(CONFIGS),
            "--distill",
            "s-1",
            "--repo",
            "git.corp/team/widget",
            "--since",
            "last tuesday",
        ]
    )

    assert code == 2
    assert "ISO-8601" in capsys.readouterr().err


def test_an_unknown_session_is_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scheduled job distilling a session that was rotated away must exit cleanly.

    Exit 4 rather than 1: the command did not fail, it was asked about something
    that does not exist, and a cron mail that cannot tell those apart is a mail
    nobody reads.
    """
    monkeypatch.setenv("FORGEPILOT_TMP", str(tmp_path / "tmp"))
    code = main(
        [
            "--config-dir",
            str(CONFIGS),
            "--distill",
            "no-such-session",
            "--repo",
            "git.corp/team/widget",
        ]
    )

    assert code == 4
    assert "no stored session" in capsys.readouterr().err
