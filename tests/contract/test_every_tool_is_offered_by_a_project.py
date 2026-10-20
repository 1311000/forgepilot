"""Every tool the registry can build must be offered by some shipped project.

⚠️ **The defect this is a guard against: `read_code`.** M19 built it, wrote its
tests, and closed the "`Retriever.open` has no caller" gap in `src/` — and then no
`tool_sets` in this repository ever named it, so no deployment could call it. The
tool existed, its tests passed, and the model was never offered it. That is the
same shape as a field nothing reads, one layer out: a capability with no
configuration that enables it.

The check is deliberately **not** "every tool is in the default set". A tool may
legitimately be opt-in; what it may not be is opt-in-and-named-nowhere, because
then nobody can opt in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forgepilot.runtime.tools.builtin import (
    build_search_definitions,
    build_tool_definitions,
)
from forgepilot.runtime.tools.mcp import built_names as mcp_names
from forgepilot.runtime.tools.subagent import built_names as subagent_names
from forgepilot.runtime.tools.web import build_web_definitions

FORGE = Path(__file__).resolve().parents[2]

#: Where a project's tool sets can live. A directory, because a new deployment
#: added here is searched without editing this test.
PROJECT_FILES = sorted(
    [*(FORGE / "configs").rglob("project.yaml"), *(FORGE / "examples").rglob("project.yaml")]
)


def _built() -> set[str]:
    """The names the registry can actually build, without a kernel.

    All three builders only close over their argument inside the tool bodies, so
    ``None`` is safe here: nothing is invoked, only the definitions are built.
    ``web_fetch`` is built from configuration (its allow-list), and the empty
    configuration is the honest one for this question — a tool that refuses every
    host is still a tool nobody can opt into if no project names it.
    """
    names = {definition.name for definition in build_tool_definitions(None)}
    names |= {definition.name for definition in build_search_definitions(None)}
    names |= {definition.name for definition in build_web_definitions({})}
    names |= subagent_names()
    # MCP needs a transport to *build*, not to name: the builder is asked for its names
    # rather than handed a client, because the question here is "does a project name it".
    names |= mcp_names()
    return names


def _offered() -> dict[str, str]:
    """Tool name → one file that offers it, over every shipped tool set."""
    offered: dict[str, str] = {}
    for path in PROJECT_FILES:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for names in (raw.get("tool_sets") or {}).values():
            for name in names or ():
                offered.setdefault(str(name), str(path.relative_to(FORGE)))
    for path in sorted((FORGE / "evals" / "tasks").glob("*/task.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        project = raw.get("project") or {}
        for names in (project.get("tool_sets") or {}).values():
            for name in names or ():
                offered.setdefault(str(name), str(path.relative_to(FORGE)))
    return offered


def test_no_tool_is_unreachable() -> None:
    built = _built()
    offered = _offered()
    # A tool set may only name tools that exist — the other direction is the one
    # `permitted_tools` reports as `unknown`, and a typo there is a silently
    # smaller tool set rather than an error.
    assert not (set(offered) - built), (
        f"these tool sets name tools that no provider builds: {sorted(set(offered) - built)}"
    )
    unoffered = sorted(built - set(offered))
    assert not unoffered, (
        f"no project in this repository offers {unoffered}: the tool can be built and "
        "can never be called. Add it to a `tool_sets` entry (opt-in is fine; "
        "unreachable is not)."
    )


def test_the_check_actually_finds_both_directions() -> None:
    """Falsification, in the same file: a name on either side of the set must show."""
    built = _built()
    offered = _offered()
    assert "read_code" in built  # the tool whose absence from `offered` was the defect
    assert "read_code" in offered  # and the fix: it is offered now
    assert offered["read_code"].endswith("project.yaml")
    assert {"read_file", "code_search", "run"} <= built


@pytest.mark.parametrize("name", ["render_diff", "summarise"])
def test_a_made_up_tool_is_neither_built_nor_offered(name: str) -> None:
    assert name not in _built()
    assert name not in _offered()
