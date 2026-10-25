"""The comparison table is a claim about two systems, and it must not rot.

`docs/notes/boundary-comparison.md` compares this system's boundary with NVIDIA
OpenShell 0.1.0. A comparison is the kind of document that keeps reading well while its
left-hand side drifts: a file gets renamed, a test is deleted, and the table still says
"enforced, see `tests/...`" about something that no longer exists. That is the defect shape
this repository keeps meeting, one level up — a claim whose evidence nobody re-checks.

Four properties, each a way this could be quietly false:

1. every row names a mechanism **and** a test that exist, and the test really contains one;
2. the verdicts come from the closed set, so a new row cannot invent "fine" or "n/a";
3. both external evidence links are present, because the right-hand side is a claim about
   somebody else's shipped design and an unsourced row is a guess;
4. the document says what it is not — a comparison without that sentence reads like a
   benchmark, and this note contains no numbers at all.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTE = ROOT / "docs" / "notes" / "boundary-comparison.md"

VERDICTS = {
    "equal",
    "ours stronger",
    "theirs stronger",
    "not comparable",
    "theirs stronger on inspection, equal on registration",
}


def _rows() -> list[list[str]]:
    body = NOTE.read_text(encoding="utf-8")
    rows: list[list[str]] = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 6 or cells[0] in {"Claim", ""} or set(cells[0]) <= {"-"}:
            continue
        rows.append(cells)
    return rows


def test_the_note_exists_and_has_rows() -> None:
    assert NOTE.is_file(), "docs/notes/boundary-comparison.md is missing"
    assert len(_rows()) >= 10, f"only {len(_rows())} comparison rows; the table shrank"


def test_every_row_names_a_mechanism_and_a_test_that_exist() -> None:
    missing: list[str] = []
    for claim, _theirs, _ours, _verdict, mechanism, test_path in _rows():
        for cell in (mechanism, test_path):
            target = ROOT / cell.strip("`")
            if not target.exists():
                missing.append(f"{claim!r} names {cell} which does not exist")
                continue
            if cell is test_path and "def test_" not in target.read_text(encoding="utf-8"):
                missing.append(f"{claim!r} names {cell}, which holds no test")
    assert not missing, "\n".join(missing)


def test_every_verdict_comes_from_the_closed_set() -> None:
    seen = {row[3] for row in _rows()}
    unknown = seen - VERDICTS
    assert not unknown, f"invented verdicts: {sorted(unknown)}"


def test_the_external_side_is_sourced() -> None:
    body = NOTE.read_text(encoding="utf-8")
    assert (
        "developer.nvidia.com/blog/add-runtime-controls-to-ai-agents-with-nvidia-openshell" in body
    )
    assert "github.com/NVIDIA/OpenShell" in body
    assert "no public material" in body, (
        "a row claims theirs is stronger without material, or the marker that says "
        "'no public material' was removed — the difference matters"
    )


def test_the_note_says_what_it_is_not() -> None:
    body = NOTE.read_text(encoding="utf-8").lower()
    assert "not a benchmark" in body
    assert "not a verdict on fitness" in body
