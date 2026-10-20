"""The milestone log is the project's memory of *what was measured*, and a move is where
records go missing.

P10 moved 37 633 bytes of milestone narrative out of `CLAUDE.md` (the harness stopped
delivering the tail at 65 536, and the front door had 18 bytes of headroom left) into
`docs/notes/milestones.md`. A move like that has an obvious failure mode nobody would
notice: the new file keeps the *heading* and loses half the bullets, and every later reader
believes a milestone that was measured was never measured — or the reverse, that a claim
still holds when the milestone that disproved it was dropped on the floor.

`test_the_workspace_instructions_fit_the_budget.py` guards the front door against loss by
size and by pointer. This module guards the *other* half: that the record the pointer leads
to still contains every milestone, and that the pointer is not the only place the split is
described.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FORGE = Path(__file__).resolve().parents[2]
FRONT_DOOR = FORGE / "CLAUDE.md"
LOG = FORGE / "docs" / "notes" / "milestones.md"

#: Every milestone the log is required to still mention. Written out rather than derived
#: from a glob: a record that a script generates can be regenerated wrong, and the point
#: of this list is that a human agreed these happened.
MILESTONES = (
    *(f"M{n}" for n in range(0, 29)),
    "M11a",
    "M11b",
)

if not FRONT_DOOR.is_file():
    pytest.skip(
        "CLAUDE.md is deliberately untracked, so a clone has no front door and no record "
        "to point at. This check earns its place on the workspace that has the instructions.",
        allow_module_level=True,
    )


def _log() -> str:
    # Not `pytest.skip`: on the box that *has* the front door, a missing log is the defect
    # this module exists for. The earlier version skipped here, and hiding the file to
    # falsify the guard made it pass — a guard that answers "absent" with "fine".
    #
    # Raised rather than asserted: the first version used `assert` and a missing file
    # surfaced as a raw `FileNotFoundError` from `read_text`, so the reader got a traceback
    # instead of the sentence saying what broke.
    if not LOG.is_file():
        raise AssertionError(
            f"{LOG.relative_to(FORGE).as_posix()} is missing, and CLAUDE.md sends a reader "
            f"to it. A dangling pointer is worse than no pointer: the reader stops looking."
        )
    return LOG.read_text(encoding="utf-8")


def test_every_milestone_is_still_named() -> None:
    text = _log()
    missing = [name for name in MILESTONES if not re.search(rf"\b{name}\b", text)]
    assert not missing, (
        f"{LOG.name} no longer mentions {missing}. The front door sends a reader here for "
        f"the record; a log with holes in it is worse than no log, because the reader has "
        f"no way to tell a gap from a milestone that never happened."
    )


def test_the_log_says_where_it_came_from() -> None:
    """A reader who lands here must be able to tell a record from a rule."""
    text = _log()
    assert text.lstrip().startswith("# "), f"{LOG.name} must open with an H1 title"
    assert "CLAUDE.md" in text, (
        f"{LOG.name} never mentions CLAUDE.md, so a reader cannot tell that it is the "
        f"record the front door points at rather than a second set of rules"
    )


def test_the_log_and_the_rules_do_not_both_fit() -> None:
    """The split's justification, pinned: one file cannot hold both, and that is measurable.

    Not a style preference. The front door is capped at 65 244 *delivered* bytes; at P10 it
    held 27 571 bytes of rules and 37 633 bytes of narrative, and the harness had already
    stopped delivering 18 bytes of it. A tidy-up that compresses the log to a stub would
    pass every other test here, and the explanation above would quietly become false.
    """
    budget = 65_244
    log = LOG.stat().st_size
    door = FRONT_DOOR.stat().st_size
    assert log > 20_000, (
        f"{LOG.name} is {log} bytes, which is not the record that was moved out of the "
        f"front door (37 633 bytes). Either the move lost content or this is a summary "
        f"standing where the record was."
    )
    assert door + log > budget, (
        f"{LOG.name} ({log} bytes) and {FRONT_DOOR.name} ({door} bytes) now fit the "
        f"{budget}-byte budget together, so the stated reason for the split no longer "
        f"holds — say what changed instead of leaving an explanation that is false"
    )


def test_the_front_door_keeps_the_heading_the_guard_requires() -> None:
    """The budget guard asserts `## State of the work` exists; so does this one.

    Cheap, and it names the coupling: `test_the_split_did_not_shrink_the_rules` in the
    budget module fails with a generic message if a later tidy-up renames the section,
    and the rename looks like a style change until you read this test.
    """
    assert FRONT_DOOR.is_file(), "CLAUDE.md is deliberately untracked; this box has it"
    text = FRONT_DOOR.read_text(encoding="utf-8")
    assert "## State of the work" in text
    assert "docs/notes/milestones.md" in text, (
        "the front door must name the log, or the only way to find the record is to "
        "already know it exists"
    )
