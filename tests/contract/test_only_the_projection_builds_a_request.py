"""One module builds a model request, and this is what makes that true.

``runtime/projection.py`` has claimed since M3 to be "the only module that
constructs a ``ChatRequest``", and for most of that time the sentence was false:
the classifier and the review narrative each built their own from hand-written
strings and called the provider directly, so two model calls left no trace the log
could rebuild. Nobody noticed, because a claim in a docstring has no reader.

The claim is enforced here instead, with ``ast`` and **no exemption list**. Two
reasons for the absence of exemptions, and they are the same reason twice: an
exemption list is a place for the next construction to hide, and it has to be
edited by the same person who is adding the offender — which is exactly the person
with an incentive not to. A third caller is not supposed to be possible; if one is
genuinely needed it belongs in :mod:`forgepilot.runtime.projection`, where the
event that makes it reconstructable is written before the call.

Written with ``ast`` rather than a regex for the same reason as
``test_import_graph.py``: a mention in a comment, a docstring or an error message
is not a construction, and must not fail the check.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PACKAGE = SRC / "forgepilot"
#: The one file allowed to build a request. Its builders each write the event that
#: makes the call reconstructable first — see ``build_side_request``.
PROJECTION = PACKAGE / "runtime" / "projection.py"


def _all_modules() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _constructions(path: Path) -> list[int]:
    """Lines where ``path`` calls ``ChatRequest(...)``, however it is spelled.

    Both spellings matter: ``ChatRequest(...)`` after a ``from ... import``, and
    ``llm.ChatRequest(...)``/``module.ChatRequest(...)`` through a namespace. A
    check that only saw the first would be bypassed by the second without anybody
    intending to bypass it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "ChatRequest":
            lines.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == "ChatRequest":
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("path", _all_modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_only_the_projection_builds_a_request(path: Path) -> None:
    if path == PROJECTION:
        pytest.skip("projection.py is the module allowed to build a request")

    offenders = _constructions(path)
    assert not offenders, (
        f"{path.relative_to(SRC)} builds a ChatRequest at line(s) {offenders}. Every "
        f"model request is built by runtime/projection.py, because every message the "
        f"model sees must carry the source_event_seq of the log event it came from — a "
        f"request built elsewhere cannot, so the call leaves nothing the log can "
        f"rebuild. Add a builder there (see build_side_request for calls that are not "
        f"part of the conversation) instead of constructing one here."
    )


def test_the_check_actually_finds_something() -> None:
    """Guard against the suite above silently passing on zero files.

    Parametrising over an empty list collects nothing and reports nothing, which
    looks identical to success — and so does a check whose pattern no longer
    matches anything, e.g. after the class is renamed. Both are pinned here.
    """
    modules = _all_modules()
    assert len(modules) > 15, f"expected the package to have many modules, found {len(modules)}"
    assert PROJECTION in modules
    assert _constructions(PROJECTION), (
        "projection.py no longer constructs a ChatRequest at all, which means this "
        "check is looking for the wrong thing rather than that the invariant held"
    )
