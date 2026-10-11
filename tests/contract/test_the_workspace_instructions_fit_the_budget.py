"""The workspace instructions must be readable **in full**, and the split is what makes
that checkable.

⚠️ **The defect this pins.** `CLAUDE.md` grew to **188315 bytes** while a harness injects
only the first **65536** of them — so the repository's most authoritative document was read
by the model up to a point in the middle of a paragraph, and everything after it was, in
practice, not a rule at all. Nothing failed; the file simply read as authoritative while
two thirds of it never arrived. That is the same shape as the `no-search` column and the
`SandboxExecutionPolicy.mode` field: a thing that looks enforced, with no reader.

The fix was a split, not a deletion: `CLAUDE.md` keeps the *rules* (30807 bytes after the
split) and `docs/notes/*.md` keeps the *evidence*. Thirteen files with no guard would drift
back within a month, so the guard is here rather than in a resolution to be careful.

Three directions, because each one fails differently:

* **the front door over budget** — the tail is silently not read (the original defect);
* **a note named but absent** — a reader is sent to a file that is not there;
* **a note present but unnamed** — nobody reads it, which is the same failure, quieter.
"""

from __future__ import annotations

import re
from pathlib import Path

FORGE = Path(__file__).resolve().parents[2]
FRONT_DOOR = FORGE / "CLAUDE.md"
NOTES_DIR = FORGE / "docs" / "notes"

#: The workspace-instruction budget: what the harness injects before truncating.
BUDGET = 65_536


def _front_door() -> str:
    return FRONT_DOOR.read_text(encoding="utf-8")


def _named_notes() -> set[str]:
    """Notes the front door points at, read out of the `docs/notes/…` references."""
    return set(re.findall(r"`docs/notes/([A-Za-z0-9._-]+\.md)`", _front_door()))


def _notes_on_disk() -> set[str]:
    return {path.name for path in NOTES_DIR.glob("*.md")}


def test_the_front_door_fits_the_budget() -> None:
    size = FRONT_DOOR.stat().st_size
    assert size <= BUDGET, (
        f"CLAUDE.md is {size} bytes and the harness injects only the first {BUDGET}, so "
        f"{size - BUDGET} bytes at the end are read by nobody. Move the evidence into "
        f"docs/notes/ and leave the rule behind."
    )


def test_every_note_the_front_door_names_exists() -> None:
    missing = sorted(name for name in _named_notes() if not (NOTES_DIR / name).is_file())
    assert not missing, (
        f"CLAUDE.md sends a reader to notes that are not there: {missing}. A dangling "
        f"pointer is worse than no pointer — the reader stops looking."
    )


def test_no_note_is_orphaned() -> None:
    orphans = sorted(_notes_on_disk() - _named_notes())
    assert not orphans, (
        f"these notes exist and CLAUDE.md never names them, so nothing leads a reader to "
        f"them: {orphans}. Add a row to the 'Where the rest lives' table, or delete the file."
    )


def test_every_note_has_a_title_and_a_way_back() -> None:
    """A note reached by accident has to say what it is and where it came from."""
    for path in sorted(NOTES_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert text.lstrip().startswith("# "), (
            f"docs/notes/{path.name} does not open with an H1 title"
        )
        assert "CLAUDE.md" in text, (
            f"docs/notes/{path.name} never mentions CLAUDE.md, so a reader who lands in it "
            f"cannot tell whether it is a rule or a record"
        )


def test_the_split_did_not_shrink_the_rules() -> None:
    """The front door must still carry the parts that are rules, not just a table of links.

    Cheap and blunt on purpose: if someone "tidies" the front door down to a stub, the
    budget test above still passes while the instructions are gone.
    """
    text = _front_door()
    for heading in (
        "## Commands",
        "## Stack decisions",
        "## Kernel semantics we depend on",
        "## Architecture",
        "## Glossary",
        "## State of the work",
    ):
        assert heading in text, f"CLAUDE.md lost its `{heading}` section"
    assert len(text) > 20_000, (
        "the front door is suspiciously small — the budget is an upper bound, not a target"
    )
