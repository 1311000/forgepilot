"""A scripted run has to be able to *ask* something, not only write and talk.

The replay model's vocabulary was ``write`` and ``say``. Neither can issue a search, so a task
whose answer is not in the checkout — the shape that finally prices retrieval, because `grep`
cannot reach it — had no offline path at all: no proof it is solvable, and no cheap regression
once it is. `search` is that third action.

⚠️ **The arguments are passed through verbatim**, which is `tool_call_turn`'s documented
purpose ("a test can script malformed arguments on purpose"). It also keeps the harness from
needing a second copy of `code_search`'s schema, which would drift from the tool's own.
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


def a_task(tmp_path: Path, script) -> H.EvalTask:
    return H.EvalTask(
        name="a-scripted-task",
        directory=tmp_path,
        fixture=tmp_path,
        ticket={},
        expect={},
        script=tuple(script),
    )


def tool_calls(turns) -> list[tuple[str, str]]:
    """Every `(tool_name, arguments)` a turn sequence asks for."""
    return [
        (chunk.tool_name, chunk.arguments_delta)
        for turn in turns
        for chunk in turn
        if chunk.kind == "tool_call_delta"
    ]


def test_a_search_step_becomes_a_code_search_call(tmp_path: Path) -> None:
    arguments = {"query": "must not repeat", "repos": ["git.corp/team/svc-a"]}
    turns = H.script_turns(a_task(tmp_path, [{"search": arguments}]))

    assert tool_calls(turns) == [("code_search", json.dumps(arguments))]


def test_the_arguments_are_not_interpreted(tmp_path: Path) -> None:
    """Verbatim, including a shape the tool would reject: the failure path is scriptable."""
    turns = H.script_turns(a_task(tmp_path, [{"search": {"nonsense": 1}}]))

    assert tool_calls(turns) == [("code_search", json.dumps({"nonsense": 1}))]


def test_search_sits_between_write_and_say(tmp_path: Path) -> None:
    """A run that reads the index, then edits, then explains — the order a real one takes."""
    (tmp_path / "solution.py").write_text("fixed = True\n", encoding="utf-8")
    turns = H.script_turns(
        a_task(
            tmp_path,
            [
                {"search": {"query": "convention"}},
                {"write": {"path": "fixed.py", "from": "solution.py"}},
                {"say": "done"},
            ],
        )
    )

    assert [name for name, _ in tool_calls(turns)] == ["code_search", "write_file"]
    assert turns[2][0].kind == "text"


def test_an_unrecognised_step_still_raises(tmp_path: Path) -> None:
    """The vocabulary grew; it did not become permissive."""
    with pytest.raises(ValueError, match="unrecognised script step"):
        H.script_turns(a_task(tmp_path, [{"teleport": 1}]))
