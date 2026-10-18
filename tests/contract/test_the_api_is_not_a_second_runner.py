"""The HTTP boundary carries tasks; it does not run them.

The workbench needs to start a task, and the shortest way to do that is to call
``run_task`` from the route that received the ticket. That would be a second
execution path — one with its own ordering, its own error handling, and its own
idea of when a session is finished — and the CLI already has one that the
evals measure. Two paths cannot be kept in step, so the difference is exactly
the sort of thing that only shows up in production.

So the runner lives in :mod:`forgepilot.httpapi.tasks` (which only *calls* it),
and this test fails if any other module under ``httpapi/`` so much as names
``plan_task`` or ``run_task``. An import is not required to break the rule: a
string passed to a registry, or a re-export, would move execution just as far.

Written with ``ast`` for the same reason the projection contract is: a mention in
a docstring or an error message is not a call, and must not fail the check.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
HTTPAPI = SRC / "forgepilot" / "httpapi"
#: The one module allowed to reach the runner: it is the service the route calls.
SERVICE = HTTPAPI / "tasks.py"
RUNNERS = frozenset({"plan_task", "run_task"})


def _modules() -> list[Path]:
    return sorted(p for p in HTTPAPI.rglob("*.py") if "__pycache__" not in p.parts)


def _mentions(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in RUNNERS:
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in RUNNERS:
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in RUNNERS:
                    found.append((node.lineno, alias.name))
    return found


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_only_the_task_service_reaches_the_runner(path: Path) -> None:
    if path == SERVICE:
        pytest.skip("tasks.py is the module allowed to call the runner")
    offenders = _mentions(path)
    assert not offenders, (
        f"{path.relative_to(SRC)} names the task runner at line(s) {offenders}. Task "
        f"mode has one execution path — `run_task` via `--task` and via the service in "
        f"httpapi/tasks.py — because a second one silently diverges from the one the "
        f"evals measure. Put the work in the service and let the route translate it."
    )


def test_the_check_actually_finds_something() -> None:
    """A parametrised check over an empty list looks exactly like a passing one."""
    modules = _modules()
    assert len(modules) > 5, f"expected the httpapi package to have modules, found {len(modules)}"
    assert SERVICE in modules
    names = {name for _, name in _mentions(SERVICE)}
    assert names == RUNNERS, (
        f"tasks.py should mention {sorted(RUNNERS)}, found {sorted(names)} — the check is "
        f"looking for the wrong thing rather than the invariant having held"
    )
