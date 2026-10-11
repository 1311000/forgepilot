"""`edit_file`: change one place instead of re-emitting the whole file.

`write_file` is a whole-file overwrite, so changing one line of an eight-hundred
line file costs the model the entire file in output tokens — and its failure mode
is silent: a truncated or dropped line reads as a successful write. Every agent that
has published on this has a targeted edit tool for exactly that reason (Claude Code's
`Edit`, Aider's five edit formats, OpenHands' `str_replace_editor`), and the two
things they all have in common are the ones tested here:

- **an exact match, or a refusal** — a near miss must never be written as though it
  matched;
- **no guessing between occurrences** — the ambiguity refusal is the tool, not a
  limitation of it.

⚠️ Two of these tests exist because of *this* project's own defects, not a generic
worry: the CRLF one (the `shlex` bug was the same shape — a Windows-only difference
that is invisible until it silently changes the wrong bytes) and the oversized-file
one (`read_file` truncates, and a truncating *write* would delete the tail).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgepilot.kernel.errors import CapabilityNotGranted
from forgepilot.runtime.tools.builtin import MAX_READ_BYTES, build_tool_definitions
from forgepilot.seams.refs import PrincipalId, RepoRef, SessionId, TenantId, TurnId
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal
from forgepilot.seams.tools import Capabilities, ToolCall, ToolContext, ToolEffect

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")

SOURCE = """\
def retry(client, credentials):
    for _ in range(3):
        try:
            return client.login(credentials)
        except AuthError:
            continue
"""


class _NoSandbox:
    """`edit_file` never reaches the sandbox; the other definitions need one."""

    def probe(self):
        raise AssertionError("edit_file must not probe a sandbox")


def _definition():
    return next(d for d in build_tool_definitions(_NoSandbox()) if d.name == "edit_file")


def _context(root: Path, *, fs_write: bool = True) -> ToolContext:
    return ToolContext(
        session_id=SessionId("s-edit"),
        constraints=ExecutionConstraints.for_principal(
            Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
            permitted_operations=frozenset({Operation.READ_CODE, Operation.WRITE_CODE}),
            writable_paths=("src",),
            network=NetworkAccess.ALLOW,
        ),
        turn=TurnId("t-1"),
        step=1,
        capabilities=Capabilities(fs_read=True, fs_write=fs_write),
        workspace_root=root,
    )


async def _edit(root: Path, **arguments):
    import json

    return await _definition().execute(
        ToolCall(call_id="c-1", name="edit_file", arguments_json=json.dumps(arguments)),
        _context(root),
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "login.py").write_bytes(SOURCE.encode("utf-8"))
    return tmp_path


def _read(root: Path) -> str:
    return (root / "src" / "login.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# It edits
# ---------------------------------------------------------------------------
async def test_a_unique_search_text_is_replaced(tree: Path) -> None:
    outcome = await _edit(
        tree, path="src/login.py", old_string="range(3)", new_string="range(MAX_ATTEMPTS)"
    )

    assert outcome.is_error is False
    assert "range(MAX_ATTEMPTS)" in _read(tree)
    assert "range(3)" not in _read(tree)
    assert outcome.meta["occurrences"] == 1


async def test_an_empty_replacement_deletes_the_text(tree: Path) -> None:
    """Deleting a line is an edit, not a missing argument."""
    outcome = await _edit(
        tree, path="src/login.py", old_string="        except AuthError:\n            continue\n",
        new_string="",
    )

    assert outcome.is_error is False
    assert "except AuthError" not in _read(tree)


async def test_the_rest_of_the_file_is_untouched_byte_for_byte(tree: Path) -> None:
    """A targeted edit that reformats its neighbours is a whole-file rewrite in a
    costume, and it is what makes a one-line change arrive as a large diff."""
    before = _read(tree).splitlines()

    await _edit(tree, path="src/login.py", old_string="range(3)", new_string="range(9)")

    after = _read(tree).splitlines()
    assert len(before) == len(after)
    assert [a for a, b in zip(before, after) if a != b] == ["    for _ in range(3):"]


async def test_replacing_every_occurrence_is_opt_in(tree: Path) -> None:
    (tree / "src" / "many.py").write_text("x = 1\ny = 1\nz = 1\n", encoding="utf-8")

    outcome = await _edit(
        tree, path="src/many.py", old_string="1", new_string="2", replace_all=True
    )

    assert outcome.meta["occurrences"] == 3
    assert (tree / "src" / "many.py").read_text(encoding="utf-8") == "x = 2\ny = 2\nz = 2\n"


# ---------------------------------------------------------------------------
# It refuses rather than guesses
# ---------------------------------------------------------------------------
async def test_an_ambiguous_search_text_is_refused_and_nothing_is_written(tree: Path) -> None:
    """⚠️ **The refusal is the feature.**

    Replacing the first occurrence silently edits a site nobody mentioned, and
    replacing all of them silently edits sites nobody mentioned. Both leave the
    caller believing a specific change was made.
    """
    (tree / "src" / "many.py").write_text("x = 1\ny = 1\n", encoding="utf-8")

    outcome = await _edit(tree, path="src/many.py", old_string="1", new_string="2")

    assert outcome.is_error is True
    assert outcome.meta["ambiguous"] is True
    assert "appears 2 times" in outcome.content
    assert "replace_all" in outcome.content, "a refusal with no way forward is a wall"
    assert (tree / "src" / "many.py").read_text(encoding="utf-8") == "x = 1\ny = 1\n"


async def test_a_search_text_that_is_not_there_says_so_and_writes_nothing(tree: Path) -> None:
    """A near miss — **one space too many** of indentation — must not be written as
    a match.

    ⚠️ And the near miss has to be real: `"for _ in range(3):"` is a *substring* of
    the indented line, so it legitimately matches, and a test asserting otherwise
    would be asserting that search/replace does not work. The first version of this
    did exactly that and failed against correct code.
    """
    before = _read(tree)

    outcome = await _edit(
        tree,
        path="src/login.py",
        old_string="     for _ in range(3):",  # five spaces; the file has four
        new_string="     for _ in range(9):",
    )

    assert outcome.is_error is True
    assert "was not found" in outcome.content
    assert "indentation" in outcome.content, "the likely cause is the actionable part"
    assert outcome.meta["occurrences"] == 0
    assert _read(tree) == before, "a miss wrote something anyway"


async def test_an_empty_search_text_is_refused(tree: Path) -> None:
    """An empty search matches at every position, so this would insert the text
    everywhere — a corruption that looks like a successful edit."""
    before = _read(tree)

    outcome = await _edit(tree, path="src/login.py", old_string="", new_string="boom")

    assert outcome.is_error is True
    assert _read(tree) == before


async def test_an_edit_that_changes_nothing_is_refused(tree: Path) -> None:
    """`old == new` is a no-op that reports as work."""
    outcome = await _edit(tree, path="src/login.py", old_string="range(3)", new_string="range(3)")

    assert outcome.is_error is True


async def test_a_missing_file_and_a_directory_are_both_actionable(tree: Path) -> None:
    missing = await _edit(tree, path="src/nope.py", old_string="a", new_string="b")
    directory = await _edit(tree, path="src", old_string="a", new_string="b")

    assert missing.is_error and "no such file" in missing.content
    assert directory.is_error and "is a directory" in directory.content
    assert directory.meta["is_directory"] is True


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------
async def test_a_path_that_escapes_the_workspace_is_refused(tree: Path) -> None:
    with pytest.raises(CapabilityNotGranted):
        await _edit(tree, path="../outside.py", old_string="a", new_string="b")


async def test_a_call_without_write_is_refused_before_it_reads(tree: Path) -> None:
    """The declaration is what the tool asked for; the capability is what it got."""
    import json

    with pytest.raises(CapabilityNotGranted):
        await _definition().execute(
            ToolCall(
                call_id="c-1",
                name="edit_file",
                arguments_json=json.dumps(
                    {"path": "src/login.py", "old_string": "range(3)", "new_string": "range(9)"}
                ),
            ),
            _context(tree, fs_write=False),
        )


async def test_an_oversized_file_is_refused_rather_than_truncated(tree: Path) -> None:
    """⚠️ **`read_file` truncates; a truncating edit would delete the tail.**

    "It edited a big file" and "it deleted everything past 512KB" would produce the
    same success message, so the refusal is the only honest answer.
    """
    big = tree / "src" / "big.py"
    big.write_bytes(b"x" * (MAX_READ_BYTES + 1))

    outcome = await _edit(tree, path="src/big.py", old_string="x", new_string="y")

    assert outcome.is_error is True
    assert outcome.meta["phase"] == "size"
    assert big.stat().st_size == MAX_READ_BYTES + 1, "the file was truncated anyway"


# ---------------------------------------------------------------------------
# Line endings — the difference a copy-paste makes invisible
# ---------------------------------------------------------------------------
async def test_a_search_text_with_the_other_line_endings_still_matches(tree: Path) -> None:
    """The model may normalise to LF what the file holds as CRLF.

    Matched on a flattened copy, spliced back into the **original bytes** — so the
    text nobody asked to change is byte-identical rather than re-serialised, which
    is the difference between a one-line edit and a whole-file rewrite.
    """
    crlf = tree / "src" / "crlf.py"
    crlf.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    outcome = await _edit(
        crlf.parent.parent, path="src/crlf.py", old_string="beta\ngamma", new_string="BETA\nGAMMA"
    )

    assert outcome.is_error is False, outcome.content
    assert outcome.meta["newlineNormalised"] is True
    # The replacement took the file's own endings, so the file is uniformly CRLF.
    assert crlf.read_bytes() == b"alpha\r\nBETA\r\nGAMMA\r\n"


async def test_an_exact_crlf_match_is_not_reported_as_normalised(tree: Path) -> None:
    """The fallback announces itself, and must stay silent when it did not run —
    otherwise a reader cannot tell a clean match from a rescued one."""
    crlf = tree / "src" / "crlf.py"
    crlf.write_bytes(b"alpha\r\nbeta\r\n")

    outcome = await _edit(
        tree, path="src/crlf.py", old_string="beta\r\n", new_string="BETA\r\n"
    )

    assert outcome.meta["newlineNormalised"] is False
    assert crlf.read_bytes() == b"alpha\r\nBETA\r\n"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_the_tool_declares_what_it_needs() -> None:
    definition = _definition()

    # READ as well as WRITE: it reads the file to match against, and `arm`
    # intersects the declaration — a narrower claim would grant an edit on a call
    # that cannot read.
    assert definition.effects == frozenset({ToolEffect.READ, ToolEffect.WRITE})
    assert definition.required_operation is Operation.WRITE_CODE
    assert definition.describe_targets is not None
    targets = definition.describe_targets({"path": "src/login.py"})
    assert targets.paths == ("src/login.py",), "a guard cannot reason about this call"


def test_both_write_tools_are_registered() -> None:
    """`edit_file` sits beside `write_file` rather than replacing it: a new file, or
    a wholesale rewrite, is still a whole-file operation."""
    names = {d.name for d in build_tool_definitions(_NoSandbox())}

    assert names == {"read_file", "write_file", "edit_file", "run"}


def test_the_shipped_project_offers_it_to_the_model() -> None:
    """⚠️ **Registered is not reachable.**

    A tool the model is never shown is dead code, and this project has that defect
    on record — a row naming a plugin nobody implemented, a field consulted by
    nothing. `project.yaml`'s `tool_sets` are what the router hands the model, so a
    writable set that can edit a file has to name both write tools or the targeted
    edit is unreachable in every deployment that copies this file.
    """
    from forgepilot.config.project import load_project

    project = load_project(Path(__file__).resolve().parents[2] / "configs" / "projects" / "example" / "project.yaml")

    writable = [name for name, tools in project.tool_sets.items() if "write_file" in tools]
    assert writable, "the fixture drifted: no tool set writes files at all"
    for name in writable:
        assert "edit_file" in project.tool_sets[name], (
            f"tool set {name!r} can write a file but cannot edit one"
        )
