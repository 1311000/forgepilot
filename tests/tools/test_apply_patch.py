"""`apply_patch`: several exact edits, one decision, all or nothing.

The tool exists because of what a single-file `edit_file` does to a change that
spans files: three calls means the state *between* them is real. If the second
fails, the first is on disk, and the next turn reasons about a tree nobody
planned. So the tests here are mostly about the refusals, and specifically about
the tree being untouched by them:

- every patch is matched before any is written, so a batch that cannot fully
  apply changes nothing;
- the failure names the patch, because "one of your eleven edits did not match"
  is a message that costs another turn to interpret;
- the guard sees *every* path, not the first one — a batch that smuggled a write
  past the writable set would be a permission defect wearing a convenience API.
"""

from __future__ import annotations

import json
from pathlib import Path

from forgepilot.runtime.tools.builtin import MAX_PATCHES, build_tool_definitions
from forgepilot.seams.refs import PrincipalId, RepoRef, SessionId, TenantId, TurnId
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal
from forgepilot.seams.tools import Capabilities, ToolCall, ToolContext, ToolEffect

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")


class _NoSandbox:
    def probe(self):
        raise AssertionError("apply_patch must not probe a sandbox")


def _definition():
    return next(d for d in build_tool_definitions(_NoSandbox()) if d.name == "apply_patch")


def _context(root: Path, *, fs_write: bool = True) -> ToolContext:
    return ToolContext(
        session_id=SessionId("s-patch"),
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


async def _patch(root: Path, patches, *, fs_write: bool = True):
    return await _definition().execute(
        ToolCall(
            call_id="c-1",
            name="apply_patch",
            arguments_json=json.dumps({"patches": patches}),
        ),
        _context(root, fs_write=fs_write),
    )


def _tree(root: Path) -> Path:
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\nNAME = 'app'\n", encoding="utf-8")
    (root / "src" / "limits.py").write_text("LIMIT = 2\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# It applies
# ---------------------------------------------------------------------------
async def test_several_files_change_in_one_call(tmp_path: Path) -> None:
    outcome = await _patch(
        _tree(tmp_path),
        [
            {"path": "src/app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"},
            {"path": "src/limits.py", "old_string": "LIMIT = 2", "new_string": "LIMIT = 3"},
        ],
    )

    assert outcome.is_error is False
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8").startswith("VALUE = 2")
    assert (tmp_path / "src" / "limits.py").read_text(encoding="utf-8") == "LIMIT = 3\n"
    assert outcome.meta["patches"] == 2
    assert sorted(outcome.meta["files"]) == ["src/app.py", "src/limits.py"]


async def test_two_patches_to_one_file_apply_in_order(tmp_path: Path) -> None:
    outcome = await _patch(
        _tree(tmp_path),
        [
            {"path": "src/app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"},
            {"path": "src/app.py", "old_string": "NAME = 'app'", "new_string": "NAME = 'svc'"},
        ],
    )

    assert outcome.is_error is False
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == (
        "VALUE = 2\nNAME = 'svc'\n"
    )
    assert outcome.meta["occurrences"] == {"src/app.py": 2}


async def test_replace_all_is_honoured(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("X = 1\nX = 1\n", encoding="utf-8")

    outcome = await _patch(
        tmp_path,
        [
            {
                "path": "src/app.py",
                "old_string": "X = 1",
                "new_string": "X = 9",
                "replace_all": True,
            }
        ],
    )

    assert outcome.is_error is False
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "X = 9\nX = 9\n"


async def test_a_crlf_file_keeps_its_line_endings(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_bytes(b"VALUE = 1\r\nNAME = 'app'\r\n")

    outcome = await _patch(
        tmp_path,
        [{"path": "src/app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"}],
    )

    assert outcome.is_error is False
    assert (tmp_path / "src" / "app.py").read_bytes() == b"VALUE = 2\r\nNAME = 'app'\r\n"


# ---------------------------------------------------------------------------
# It refuses, and the tree proves it
# ---------------------------------------------------------------------------
async def test_one_bad_patch_writes_nothing(tmp_path: Path) -> None:
    root = _tree(tmp_path)

    outcome = await _patch(
        root,
        [
            {"path": "src/app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"},
            {"path": "src/limits.py", "old_string": "LIMIT = 99", "new_string": "LIMIT = 3"},
        ],
    )

    assert outcome.is_error is True
    assert "patch 2 of 2" in outcome.content
    assert "Nothing was written" in outcome.content
    # The first patch matched; the file must still be untouched.
    assert (root / "src" / "app.py").read_text(encoding="utf-8").startswith("VALUE = 1")


async def test_an_ambiguous_match_is_refused_and_points_at_replace_all(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("X = 1\nX = 1\n", encoding="utf-8")

    outcome = await _patch(
        tmp_path,
        [{"path": "src/app.py", "old_string": "X = 1", "new_string": "X = 9"}],
    )

    assert outcome.is_error is True
    assert "appears 2 times" in outcome.content
    assert "replace_all" in outcome.content
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "X = 1\nX = 1\n"


async def test_a_missing_file_is_named_rather_than_created(tmp_path: Path) -> None:
    outcome = await _patch(
        _tree(tmp_path),
        [{"path": "src/new.py", "old_string": "a", "new_string": "b"}],
    )

    assert outcome.is_error is True
    assert "no such file: src/new.py" in outcome.content
    assert "write_file" in outcome.content
    assert not (tmp_path / "src" / "new.py").exists()


async def test_an_empty_search_text_is_refused(tmp_path: Path) -> None:
    outcome = await _patch(
        _tree(tmp_path),
        [{"path": "src/app.py", "old_string": "", "new_string": "x"}],
    )

    assert outcome.is_error is True
    assert "non-empty" in outcome.content


async def test_an_identical_pair_is_refused(tmp_path: Path) -> None:
    outcome = await _patch(
        _tree(tmp_path),
        [{"path": "src/app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 1"}],
    )

    assert outcome.is_error is True
    assert "identical" in outcome.content


async def test_a_directory_is_refused(tmp_path: Path) -> None:
    outcome = await _patch(
        _tree(tmp_path),
        [{"path": "src", "old_string": "a", "new_string": "b"}],
    )

    assert outcome.is_error is True
    assert "directory, not a file" in outcome.content


async def test_a_path_outside_the_workspace_is_refused_by_the_tool(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    outcome = await _patch(
        tmp_path,
        [
            {"path": "src/app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"},
            {"path": "../outside.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"},
        ],
    )

    # Escaping the workspace is the tool's own refusal, not the guard's: the
    # pipeline's path guard also refuses it, and an answer the model can read
    # beats an exception that only the pipeline can translate.
    assert outcome.is_error is True
    assert "not a path inside the workspace" in outcome.content
    assert "Nothing was written" in outcome.content
    assert outside.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_a_path_outside_the_writable_set_is_still_reported_to_the_guard() -> None:
    # Writability is the path guard's job, not the tool's: the tool cannot know
    # the policy, and duplicating the check here would let the two disagree.
    # What the tool owes the guard is the full list of paths it would write.
    definition = _definition()

    targets = definition.describe_targets(
        {
            "patches": [
                {"path": "README.md"},
                {"path": "src/app.py"},
            ]
        }
    )

    assert targets.paths == ("README.md", "src/app.py")


async def test_a_batch_larger_than_the_bound_is_refused(tmp_path: Path) -> None:
    patches = [
        {"path": "src/app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"}
        for _ in range(MAX_PATCHES + 1)
    ]

    outcome = await _patch(_tree(tmp_path), patches)

    assert outcome.is_error is True
    assert "more than this tool applies" in outcome.content


# ---------------------------------------------------------------------------
# What the pipeline and the router see
# ---------------------------------------------------------------------------
def test_the_guard_is_shown_every_path_not_the_first() -> None:
    definition = _definition()

    targets = definition.describe_targets(
        {
            "patches": [
                {"path": "src/a.py"},
                {"path": "src/b.py"},
            ]
        }
    )

    assert targets.paths == ("src/a.py", "src/b.py")


def test_it_is_a_read_and_a_write_tool() -> None:
    definition = _definition()

    assert definition.effects == frozenset({ToolEffect.READ, ToolEffect.WRITE})
    assert definition.required_operation is Operation.WRITE_CODE
