"""The model is reached through the guard, not around it.

``runtime/log_guard.py`` exists because "remember to flush first" is a rule that a
new code path eventually forgets: sending a request while events are still buffered
means a crash loses what the model was told, and the log then disagrees with
reality. It has one caller-shaped hole it cannot close from the inside — a caller
that has an ``llm`` and calls it directly.

Measured before M16, ``grep -rn "\\.complete(\\|\\.stream(" src/`` found exactly two
such calls, the classifier and the review narrative, and neither was guarded.
Both now go through ``guarded_complete``. This test is what keeps a third from
arriving quietly.

The check is deliberately narrow — an ``llm.<method>(...)`` call — and that is not
a concession, it is the same property as ``test_only_the_projection_builds_a_request``
seen from the other side: a call that the log cannot rebuild needs a request, and a
request can only be built in ``projection.py``, which logs the call before it
happens. So the shapes that can actually lose evidence are (a) a hand-built request
and (b) a direct call on something named as the model. Both are checked; a
``self.stream(...)`` inside a provider, or an HTTP client's ``client.stream(...)``,
is neither, and needs no exemption.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PACKAGE = SRC / "forgepilot"
#: The guard itself. This is the only place allowed to call the model directly.
LOG_GUARD = PACKAGE / "runtime" / "log_guard.py"
_GUARDED_METHODS = frozenset({"complete", "stream"})


def _all_modules() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _unguarded_calls(path: Path) -> list[tuple[int, str]]:
    """``llm.complete(...)`` / ``llm.stream(...)`` calls, as ``(lineno, source)``.

    The receiver is read from the syntax tree rather than matched as text, so
    ``guarded_complete(...)`` — a free function whose name merely *ends* the same
    way — is not an offender, and neither is a call on a client or on ``self``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _GUARDED_METHODS:
            continue
        receiver = ast.unparse(node.func.value)
        if receiver.rsplit(".", 1)[-1] == "llm":
            found.append((node.lineno, f"{receiver}.{node.func.attr}(...)"))
    return found


@pytest.mark.parametrize("path", _all_modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_the_model_is_reached_through_the_guard(path: Path) -> None:
    if path == LOG_GUARD:
        pytest.skip("log_guard.py is the module that makes the call")

    offenders = _unguarded_calls(path)
    assert not offenders, (
        f"{path.relative_to(SRC)} calls the model directly at line(s) {offenders}. Use "
        f"runtime/log_guard.guarded_complete/guarded_stream — they flush first, so the "
        f"events the model saw are durable before it is asked anything. If the call is "
        f"not part of the conversation, build its request with "
        f"projection.build_side_request, which records the prompt before the call."
    )


def test_the_check_actually_finds_something() -> None:
    """Guard against a check that has quietly stopped matching anything.

    Two ways this suite could pass while enforcing nothing: parametrising over an
    empty file list, and a pattern that no longer matches the guard it is supposed
    to permit. Both are pinned — the second one is the interesting one, because a
    renamed method would otherwise turn this file into decoration.
    """
    modules = _all_modules()
    assert len(modules) > 15, f"expected the package to have many modules, found {len(modules)}"
    assert LOG_GUARD in modules
    assert _unguarded_calls(LOG_GUARD), (
        "log_guard.py no longer calls the model at all, which means this check is "
        "looking for the wrong thing rather than that the invariant held"
    )
