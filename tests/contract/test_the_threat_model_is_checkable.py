"""The threat model is a table, and a table nobody checks drifts into a story.

`docs/notes/threat-model.md` claims a mechanism and a test for every security-relevant
sentence this project makes. This test is what makes the claim checkable: a row whose test
file is missing, whose module is missing, or whose status is not one of the three words is a
row a reader would trust and should not.

The rows are parsed out of the markdown rather than duplicated here, so adding a claim
without adding a test fails this test rather than passing silently.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "notes" / "threat-model.md"

STATUSES = {"enforced", "partial", "declaration"}
#: A status cell may carry a qualifier after the word ("partial — on Windows").
_STATUS = re.compile(r"^(enforced|partial|declaration)\b")
#: `| claim | mechanism | `path` | `test path` | status |` — the five-column table.
_ROW = re.compile(
    r"^\|(?P<claim>[^|]+)\|(?P<mechanism>[^|]+)\|(?P<where>[^|]+)\|(?P<test>[^|]+)\|(?P<status>[^|]+)\|$"
)
_BACKTICK = re.compile(r"`([^`]+)`")


def _rows() -> list[dict[str, str]]:
    text = DOC.read_text(encoding="utf-8")
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        cells = {key: value.strip() for key, value in match.groupdict().items()}
        parsed = _STATUS.match(cells["status"])
        if not parsed:
            continue  # the header and the separator have no status word
        rows.append({**cells, "status": parsed.group(1), "status_text": cells["status"]})
    return rows


def test_the_document_exists_and_has_rows() -> None:
    assert DOC.is_file(), (
        "docs/notes/threat-model.md is missing; the notes table in CLAUDE.md sends a reader to it"
    )
    assert len(_rows()) >= 10, f"only {len(_rows())} claims; a threat model with fewer is a slogan"


def test_every_status_is_one_of_the_three_words() -> None:
    for row in _rows():
        assert row["status"] in STATUSES, f"{row['claim']!r} has status {row['status']!r}"
        # A row that is not `enforced` has to say where the line is, in the same cell:
        # "partial — on Windows" is a claim a reader can act on, "partial" is a shrug.
        if row["status"] != "enforced":
            assert "—" in row["status_text"] or "(" in row["status_text"], (
                f"{row['claim']!r} is {row['status']} without saying where the line is"
            )


def test_every_claim_names_a_test_that_exists_and_has_a_test_function() -> None:
    missing: list[str] = []
    empty: list[str] = []
    for row in _rows():
        names = _BACKTICK.findall(row["test"])
        assert names, f"{row['claim']!r} names no test file"
        for name in names:
            path = ROOT / name
            if not path.is_file():
                missing.append(name)
                continue
            if "def test_" not in path.read_text(encoding="utf-8"):
                empty.append(name)
    assert not missing, (
        f"threat-model rows name test files that do not exist: {sorted(set(missing))}"
    )
    assert not empty, f"threat-model rows name files with no test in them: {sorted(set(empty))}"


def test_every_claim_names_a_module_that_exists() -> None:
    missing: list[str] = []
    for row in _rows():
        for name in _BACKTICK.findall(row["where"]):
            if name.startswith(("tests/", "docs/")):
                continue
            if not (ROOT / name).exists():
                missing.append(name)
    assert not missing, f"threat-model rows name modules that do not exist: {sorted(set(missing))}"


def test_the_document_names_the_assumptions_it_does_not_defend() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "## Assumptions" in text
    for phrase in ("multi-replica", "network filesystem", "RS256"):
        assert phrase in text, f"the threat model no longer names the assumption about {phrase!r}"


def test_a_declaration_row_is_not_a_synonym_for_planned() -> None:
    """The word has to be defined in the document, because it carries the honesty."""
    text = DOC.read_text(encoding="utf-8")
    assert "**Nothing is prevented**" in text, (
        "the status table no longer says that `declaration` prevents nothing"
    )
