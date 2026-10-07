"""Mechanical enforcement of the architectural rules that are otherwise advice.

Four rules, each of which decays the moment it is only documented:

1. Only ``kernel/`` may import ``cordis_port`` — otherwise the plugin kernel is
   no longer replaceable, which is the entire reason it was isolated.
2. Nothing may import a ``dsh_*`` package — DeepSeek Harness is an architectural
   *reference*, not a dependency; importing it would silently reverse a settled
   decision.
3. ``seams/`` may not import ``providers/`` or ``cordis_port`` — a seam that
   knows how a capability is hosted is not a seam.
4. A provider may not import a *different* provider — two providers that need to
   cooperate must do so through a service, or neither is swappable.

Written with ``ast`` rather than a regex so that a commented-out or string
mention of an import does not trip it, and a real one cannot hide.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
PACKAGE = SRC / "forgepilot"


def _module_name(path: Path) -> str:
    """``src/forgepilot/providers/llm/replay.py`` -> ``forgepilot.providers.llm.replay``."""
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@dataclass(frozen=True, slots=True)
class Import:
    module: str
    lineno: int
    raw: str


def _imports(path: Path) -> list[Import]:
    """Every module imported by ``path``, with relative imports resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    own = _module_name(path)
    own_package = own.rsplit(".", 1)[0] if "." in own else own
    found: list[Import] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(Import(alias.name, node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                target = node.module or ""
            else:
                parts = own_package.split(".") if own_package else []
                # level 1 means "this package", level 2 means "the parent", ...
                keep = len(parts) - (node.level - 1)
                base = ".".join(parts[: max(keep, 0)])
                target = f"{base}.{node.module}" if node.module else base
            found.append(
                Import(target, node.lineno, f"from {'.' * node.level}{node.module or ''} import ...")
            )
    return found


def _all_modules() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _provider_group(module: str) -> str | None:
    """The provider family a module belongs to, e.g. ``llm`` for
    ``forgepilot.providers.llm.replay``. ``None`` for the package itself."""
    parts = module.split(".")
    if len(parts) >= 3 and parts[:2] == ["forgepilot", "providers"]:
        return parts[2]
    return None


@pytest.mark.parametrize("path", _all_modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_only_kernel_imports_cordis_port(path: Path) -> None:
    if path.relative_to(PACKAGE).parts[0] == "kernel":
        pytest.skip("kernel/ is the one package allowed to import cordis_port")

    offenders = [imp for imp in _imports(path) if imp.module.split(".")[0] == "cordis_port"]
    assert not offenders, (
        f"{path.relative_to(SRC)} imports cordis_port at line(s) "
        f"{[i.lineno for i in offenders]}. Go through forgepilot.kernel instead — it is "
        f"the only package permitted to know the kernel exists."
    )


@pytest.mark.parametrize("path", _all_modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_nothing_imports_deepseek_harness(path: Path) -> None:
    offenders = [
        imp
        for imp in _imports(path)
        if imp.module.split(".")[0].startswith("dsh") or "deepseek_harness" in imp.module
    ]
    assert not offenders, (
        f"{path.relative_to(SRC)} imports a DeepSeek Harness package at line(s) "
        f"{[i.lineno for i in offenders]}. DSH is an architectural reference, not a "
        f"dependency; re-express the mechanism as a ForgePilot-owned Protocol instead."
    )


@pytest.mark.parametrize("path", _all_modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_seams_do_not_import_providers(path: Path) -> None:
    if path.relative_to(PACKAGE).parts[0] != "seams":
        pytest.skip("not a seam module")

    offenders = [imp for imp in _imports(path) if imp.module.startswith("forgepilot.providers")]
    assert not offenders, (
        f"{path.relative_to(SRC)} imports a provider at line(s) "
        f"{[i.lineno for i in offenders]}. A seam that knows its implementation is not "
        f"replaceable."
    )


@pytest.mark.parametrize("path", _all_modules(), ids=lambda p: str(p.relative_to(SRC)))
def test_providers_do_not_import_other_providers(path: Path) -> None:
    own_group = _provider_group(_module_name(path))
    if own_group is None:
        pytest.skip("not inside a provider family")

    offenders = []
    for imp in _imports(path):
        other = _provider_group(imp.module)
        if other is not None and other != own_group:
            offenders.append(imp)

    assert not offenders, (
        f"{path.relative_to(SRC)} imports a different provider family at line(s) "
        f"{[(i.lineno, i.module) for i in offenders]}. Providers cooperate through "
        f"services, never through imports."
    )


def test_the_checks_actually_find_something() -> None:
    """Guard against the suites above silently passing on zero files.

    A parametrised test over an empty list collects nothing and reports nothing,
    which looks identical to success.
    """
    modules = _all_modules()
    assert len(modules) > 15, f"expected the package to have many modules, found {len(modules)}"
    assert any(_provider_group(_module_name(p)) == "llm" for p in modules)
    assert any(_module_name(p).startswith("forgepilot.seams") for p in modules)
