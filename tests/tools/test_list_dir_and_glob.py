"""`list_dir` and `glob`: the two ways to ask "what is here".

Neither is a read of content, and both exist because of a measured failure rather
than a wish for completeness. M28's `no-shell` arm removed `run`, and the agent
found files it had no business finding by asking `read_file` for a **directory** —
which listed it. Discovery therefore worked, but it worked by accident, through a
tool whose contract says "one file" and whose error message happened to be useful.

Two consequences shaped these tools:

- `list_dir` on the root is a legitimate request. The path guard refuses anything
  that normalises to ``None``, and ``"."`` normalises to ``None`` — the root is not
  a file anyone can address — so the definition's target for the default has to be
  *no target* rather than a target the guard would read as an escape.
- A symlink may point out of the workspace. The guard judged the pattern, not what
  the pattern resolved to, so the tool itself drops matches that resolve outside.
  Skipping is the only answer that neither leaks the target nor claims the file is
  not there.
"""

from __future__ import annotations

import json
from pathlib import Path

from forgepilot.runtime.tools.builtin import build_tool_definitions
from forgepilot.seams.refs import PrincipalId, RepoRef, SessionId, TenantId, TurnId
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal
from forgepilot.seams.tools import Capabilities, ToolCall, ToolContext, ToolEffect

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")


class _NoSandbox:
    """Neither tool reaches the sandbox; the other definitions need one."""

    def probe(self):
        raise AssertionError("list_dir and glob must not probe a sandbox")


def _definition(name: str):
    return next(d for d in build_tool_definitions(_NoSandbox()) if d.name == name)


def _context(root: Path, *, fs_read: bool = True) -> ToolContext:
    return ToolContext(
        session_id=SessionId("s-list"),
        constraints=ExecutionConstraints.for_principal(
            Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
            permitted_operations=frozenset({Operation.READ_CODE}),
            network=NetworkAccess.ALLOW,
        ),
        turn=TurnId("t-1"),
        step=1,
        capabilities=Capabilities(fs_read=fs_read),
        workspace_root=root,
    )


async def _call(name: str, root: Path, **arguments):
    definition = _definition(name)
    return await definition.execute(
        ToolCall(call_id="c-1", name=name, arguments_json=json.dumps(arguments)),
        _context(root),
    )


def _tree(root: Path) -> Path:
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src" / "pkg" / "limits.py").write_text("LIMIT = 2\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test(): pass\n", encoding="utf-8")
    (root / "README.md").write_text("# x\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------
async def test_the_default_directory_is_the_root(tmp_path: Path) -> None:
    outcome = await _call("list_dir", _tree(tmp_path))

    assert outcome.is_error is False
    assert "src/" in outcome.content
    assert "tests/" in outcome.content
    assert "README.md" in outcome.content


async def test_directories_come_first_and_are_marked(tmp_path: Path) -> None:
    outcome = await _call("list_dir", _tree(tmp_path))

    lines = outcome.content.splitlines()[1:]
    assert lines[0] == "src/"
    assert lines[1] == "tests/"
    assert lines[2] == "README.md"


async def test_a_subdirectory_lists_with_its_path(tmp_path: Path) -> None:
    outcome = await _call("list_dir", _tree(tmp_path), path="src")

    assert "src: 2 entries" in outcome.content
    assert "src/pkg/" in outcome.content
    assert "src/app.py" in outcome.content


async def test_a_file_is_refused_and_points_at_read_file(tmp_path: Path) -> None:
    outcome = await _call("list_dir", _tree(tmp_path), path="README.md")

    assert outcome.is_error is True
    assert "read_file" in outcome.content


async def test_a_missing_directory_says_so(tmp_path: Path) -> None:
    outcome = await _call("list_dir", _tree(tmp_path), path="src/nowhere")

    assert outcome.is_error is True
    assert outcome.content.startswith("no such directory")


async def test_the_escape_is_refused_by_the_tool_as_well(tmp_path: Path) -> None:
    """The guard refuses it first; the tool must not depend on that."""
    outcome = await _call("list_dir", _tree(tmp_path), path="../outside")

    assert outcome.is_error is True
    assert "refused" in outcome.content or "outside" in outcome.content


async def test_the_listing_is_bounded_and_says_so(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"f{index}.txt").write_text("x", encoding="utf-8")

    outcome = await _call("list_dir", tmp_path, max_entries=2)

    assert outcome.meta["shown"] == 2
    assert outcome.meta["truncated"] is True
    assert "(truncated)" in outcome.content


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------
async def test_a_glob_finds_files_at_depth(tmp_path: Path) -> None:
    outcome = await _call("glob", _tree(tmp_path), pattern="**/*.py")

    assert outcome.is_error is False
    assert "src/app.py" in outcome.content
    assert "src/pkg/limits.py" in outcome.content
    assert "tests/test_app.py" in outcome.content
    assert "README.md" not in outcome.content


async def test_a_glob_in_one_directory_does_not_reach_below(tmp_path: Path) -> None:
    outcome = await _call("glob", _tree(tmp_path), pattern="src/*.py")

    assert "src/app.py" in outcome.content
    assert "src/pkg/limits.py" not in outcome.content


async def test_a_glob_with_no_match_says_zero_rather_than_failing(tmp_path: Path) -> None:
    outcome = await _call("glob", _tree(tmp_path), pattern="**/*.rs")

    assert outcome.is_error is False
    assert outcome.meta["matches"] == 0
    assert "0 matches" in outcome.content


async def test_a_missing_pattern_is_a_refusal(tmp_path: Path) -> None:
    outcome = await _call("glob", _tree(tmp_path))

    assert outcome.is_error is True
    assert "pattern" in outcome.content


async def test_a_glob_cannot_escape_the_workspace(tmp_path: Path) -> None:
    outcome = await _call("glob", _tree(tmp_path), pattern="../**/*.py")

    assert outcome.is_error is True
    assert "not a pattern inside the workspace" in outcome.content


async def test_the_glob_is_bounded_and_says_so(tmp_path: Path) -> None:
    outcome = await _call("glob", _tree(tmp_path), pattern="**/*.py", max_results=1)

    assert outcome.meta["matches"] == 1
    assert outcome.meta["truncated"] is True
    assert "(truncated)" in outcome.content


# ---------------------------------------------------------------------------
# Both are offered by name, and neither claims a capability it lacks
# ---------------------------------------------------------------------------
def test_both_are_definitions_the_router_can_offer() -> None:
    names = {definition.name for definition in build_tool_definitions(_NoSandbox())}

    assert {"list_dir", "glob"} <= names


def test_both_are_reads() -> None:
    for name in ("list_dir", "glob"):
        definition = _definition(name)
        assert definition.required_operation is Operation.READ_CODE
        assert definition.effects == frozenset({ToolEffect.READ})
