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

⚠️ **This is a workspace guard, not a checkout guard.** `CLAUDE.md` is deliberately untracked
(`.gitignore`): it is written for whoever works in *this* directory, and it was removed from
the history along with the two documents it names. A clone therefore has no front door to
read, and the module skips there rather than failing a checkout for a file it was never
supposed to contain. The check earns its place on the machine that has the instructions.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

FORGE = Path(__file__).resolve().parents[2]
FRONT_DOOR = FORGE / "CLAUDE.md"
NOTES_DIR = FORGE / "docs" / "notes"

#: What the harness *documents*: it injects the first 65 536 bytes of `CLAUDE.md`.
HARNESS_CAP = 65_536

#: The bytes the harness spends wrapping that content before it reaches the model.
#: **Measured, not assumed.** When the front door stood at 65 503 bytes the harness
#: reported `truncated CLAUDE.md from 65503 to 65244 bytes` — so the last 259 bytes of
#: *content* never arrived, while the nominal cap still showed 33 bytes of headroom. The
#: earlier constant here was `65_536`, the nominal cap, which meant the gate was green
#: for a stretch of the file that no reader received: the same shape as every other row
#: in `docs/notes/defect-shape.md` — a number that looks like a constraint with nothing
#: measuring the thing it constrains.
#:
#: To re-measure: grow this file to just under the cap and read the harness's own
#: `truncated … to N bytes` notice. If that notice disappears, the wrapper shrank and
#: this number is stale in the safe direction (the gate is stricter than the harness).
WRAPPER = 292

#: The budget the gate actually enforces: bytes of the file that a reader receives.
BUDGET = HARNESS_CAP - WRAPPER

if not FRONT_DOOR.exists():
    pytest.skip(
        f"{FRONT_DOOR.name} is not in this checkout — it is deliberately untracked "
        f"(`.gitignore`), so there is no front door to check here. Run this in a workspace "
        f"that carries the instructions.",
        allow_module_level=True,
    )


def _front_door() -> str:
    return FRONT_DOOR.read_text(encoding="utf-8")


def _named_notes() -> set[str]:
    """Notes the front door points at, read out of the `docs/notes/…` references."""
    return set(re.findall(r"`docs/notes/([A-Za-z0-9._-]+\.md)`", _front_door()))


def _notes_on_disk() -> set[str]:
    return {path.name for path in NOTES_DIR.glob("*.md")}


#: Where a path written in the front door may be relative to. Package-relative
#: references (`runtime/projection.py`, `providers/sandbox/*`) are how the front door
#: has always written them, so the check resolves them the way a reader would.
_REFERENCE_BASES = ("", "src/forgepilot/", "tests/", "evals/")

_BACKTICKED = re.compile(r"`([^`\n]+)`")
_BRACE_GROUP = re.compile(r"\{([^{}]*)\}")
_FILE_TAIL = re.compile(r"\.(?:md|py|ya?ml|json|toml|html|txt|sh)$")


def _expanded(reference: str) -> set[str]:
    """`runtime/{events,session}.py` names two paths; `providers/sandbox/*` names a set."""
    group = _BRACE_GROUP.search(reference)
    if group is None:
        return {reference}
    head, _, tail = reference[: group.start()], group.group(1), reference[group.end() :]
    return {f"{head}{part}{tail}" for part in group.group(1).split(",")}


def _referenced_paths() -> set[str]:
    """Paths the front door claims exist, read out of its backticked references.

    Only things that look like a *path* — a slash, and a file extension or a glob. A
    bare filename (`ci.yml`, `pyproject.toml`) is not a location claim, and the commands
    in the fenced block below are the shell's business, not the guard's.
    """
    found: set[str] = set()
    for token in _BACKTICKED.findall(_front_door()):
        token = token.strip()
        if " " in token or token.startswith(("http", "-", ".venv", "~", "$")):
            continue
        if "/" not in token or not (_FILE_TAIL.search(token) or "*" in token):
            continue
        found.update(_expanded(token))
    return found


def _resolves(reference: str) -> bool:
    if "*" in reference:
        return any(any(FORGE.glob(f"{base}{reference}")) for base in _REFERENCE_BASES)
    return any((FORGE / f"{base}{reference}").exists() for base in _REFERENCE_BASES)


def test_the_front_door_fits_the_budget() -> None:
    size = FRONT_DOOR.stat().st_size
    assert size <= BUDGET, (
        f"CLAUDE.md is {size} bytes and the harness delivers only {BUDGET} of them "
        f"({HARNESS_CAP} minus the {WRAPPER}-byte wrapper it adds), so {size - BUDGET} "
        f"bytes at the end are read by nobody. Move the evidence into docs/notes/ and "
        f"leave the rule behind."
    )


def test_the_budget_is_the_delivered_size_not_the_documented_cap() -> None:
    """Pin the arithmetic, so a later tidy-up cannot restore the number that was wrong.

    The defect this whole module exists for was "a cap with nothing measuring delivery".
    Setting the constant back to `HARNESS_CAP` would recreate it in the guard itself:
    green for up to 292 bytes that never arrive.
    """
    assert BUDGET == HARNESS_CAP - WRAPPER
    assert 0 < WRAPPER < HARNESS_CAP
    assert BUDGET < HARNESS_CAP, (
        "the enforced budget must be strictly below the documented cap — the wrapper is "
        "not free, and a guard that ignores it is green about text nobody reads"
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


def test_every_path_the_front_door_names_exists() -> None:
    """A pointer the reader follows to nothing is worse than no pointer.

    The split moved evidence out of this file, so the front door is now mostly
    *references* — which means its failure mode changed from "the tail is not read" to
    "the reader is sent nowhere". Renaming or deleting a module leaves the instruction
    reading exactly as authoritative as before.
    """
    dangling = sorted(reference for reference in _referenced_paths() if not _resolves(reference))
    assert not dangling, (
        f"CLAUDE.md names these paths and they are not there: {dangling}. Fix the "
        f"reference or the tree — do not leave a reader following a dead pointer."
    )


def test_no_document_is_unnamed() -> None:
    """Every document the repository keeps must be reachable from the front door.

    `docs/实现方案.md` sat in the repository for the whole project and no instruction
    named it: two Chinese design documents, one referenced and one not, with nothing
    saying how they relate. An orphan note and an orphan document are the same defect.
    """
    documents = sorted(path for path in (FORGE / "docs").rglob("*.md"))
    documents += sorted(FORGE.glob("*.md"))
    exempt = {FORGE / "CLAUDE.md", FORGE / "README.md"}
    unnamed = [
        path
        for path in documents
        if path not in exempt and path.relative_to(FORGE).as_posix() not in _front_door()
    ]
    assert not unnamed, (
        f"these documents exist and CLAUDE.md never names them: "
        f"{[path.relative_to(FORGE).as_posix() for path in unnamed]}. Name them where a "
        f"reader would look, or delete them."
    )

def test_the_path_check_notices_a_missing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the counterfactual: a guard that has only ever been green is a guess.

    It reads the tree with the references doctored, so this fails if
    `_referenced_paths`/`_resolves` stop being able to tell a real path from a dead one.
    """
    doctored = "see `docs/notes/no-such-note.md` and `runtime/{gone,also_gone}.py`"
    monkeypatch.setattr(sys.modules[__name__], "_front_door", lambda: doctored)
    references = _referenced_paths()
    assert references == {
        "docs/notes/no-such-note.md",
        "runtime/gone.py",
        "runtime/also_gone.py",
    }, f"the front door's references were read wrong: {references}"
    assert not any(_resolves(reference) for reference in references)


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
