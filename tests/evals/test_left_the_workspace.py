"""Does the eval notice when a run read its own answers?

The mechanism exists because a run that reached `evals/tasks/<name>/solution/`
is **not evidence about the agent**, and nothing used to say so: the harness
scored it, the report counted it, and the number looked like every other number.

These tests pin both directions. The silent cases matter as much as the firing
ones — a detector that cries wolf on honest work gets switched off, and then it
protects nothing. One of them is not hypothetical: it is the command the first
version actually fired on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import harness as H  # noqa: E402

SCRATCH = H.scratch_root()
INTERPRETER = str(FORGE / ".venv" / "Scripts" / "python.exe")


def run_call(command: str, seq: int = 1):
    from forgepilot.seams.session import SessionEvent, SurfaceOp

    return SessionEvent(
        seq=seq,
        kind="tool/call",
        data={"callId": "c1", "name": "run", "arguments": json.dumps({"command": command})},
        surface=SurfaceOp.LOG_ONLY,
    )


def findings_for(command: str) -> tuple[str, ...]:
    return H.tool_calls_that_left_the_workspace(
        [run_call(command)], scratch=SCRATCH, interpreter=INTERPRETER
    )


# ---------------------------------------------------------------------------
# The cases it exists for
# ---------------------------------------------------------------------------
def test_a_command_that_cds_into_the_project_is_caught() -> None:
    """The real one. `session-20261010T024749` did exactly this and then searched
    the checkout for `stats.py` — a different copy of the file it was editing.
    """
    found = findings_for(f'cd /d "{FORGE}" && dir /s /b')
    assert found, "a command that moved into the project root was not caught"
    assert str(FORGE) in found[0]


def test_a_command_that_names_a_solution_is_caught() -> None:
    solution = FORGE / "evals" / "tasks" / "out-of-scope" / "solution" / "metrics.py"
    found = findings_for(f'type "{solution}"')
    assert found, "a command that read a task's own solution was not caught"


def test_score_fails_a_row_that_left_the_checkout() -> None:
    """A finding beside a pass is a pass. The row has to stop counting."""
    task = H.load_task("bug-fix")
    row = {
        "state": "ready",
        "checks_passed": True,
        "changed": [],
        "tool_calls": 3,
        "turns": 1,
        "left_the_workspace": ["seq 8: references 'C:\\…\\forge' :: cd /d C:\\…\\forge"],
    }
    failures = H.score(row, task)
    assert any("outside the checkout" in f for f in failures), failures


def test_a_row_that_stayed_put_is_not_failed_for_it() -> None:
    task = H.load_task("bug-fix")
    row = {
        "state": "ready",
        "checks_passed": True,
        "changed": [],
        "tool_calls": 3,
        "turns": 1,
        "left_the_workspace": [],
    }
    assert not [f for f in H.score(row, task) if "outside the checkout" in f]


# ---------------------------------------------------------------------------
# The cases it must stay silent on — each one is a mistake the design could make
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    [
        "dir /s /b",
        r"type src\metrics.py",
        "dir /b & echo --- & dir /b src tests",
        'cd /d "%CD%" && dir /s /b',
        "python -m pytest -q 2>&1 | tail -30",
        # ⚠️ The measured false positive. `cd src` then `cd ..` comes back to the
        # checkout's root; the first version flagged it, on the reasoning that the
        # shell starts at the root and `..` therefore leaves. Both halves are true
        # and the conclusion is still wrong.
        'cd src && python -c "import settings" ; cd .. && python -c "import settings"',
    ],
)
def test_ordinary_commands_are_not_flagged(command: str) -> None:
    assert findings_for(command) == (), f"{command!r} was flagged"


def test_the_projects_own_check_command_is_not_flagged() -> None:
    """`${FORGEPILOT_PYTHON} -m pytest -q` expands to a path inside the project,
    and the model is *told* that path by the check output. Flagging it would make
    every run that re-ran the tests a failure.
    """
    assert findings_for(f'"{INTERPRETER}" -m pytest -q') == ()


def test_slash_switches_are_not_read_as_paths() -> None:
    """`dir /s /b` has two switches. A one-separator rule reads them as POSIX
    paths — which is why `_ABSOLUTE_POSIX` requires a second slash.
    """
    assert findings_for("dir /s /b /a-d") == ()
