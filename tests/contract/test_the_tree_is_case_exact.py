"""Every import in this tree works on Linux, where `Retriever.py` and `retriever.py` are two files.

`docs/notes/gates-on-linux.md`'s N5 recorded a hand audit: 934 intra-project
`forgepilot.*` imports, no relative imports, no uppercase path component under
`src/ tests/ tools/ evals/ examples/`, no case-only twins, no `pythonpath`. It
concluded *proved absent, not sampled* — and then wrote down its own expiry:
"a new import path containing a capital, or a rename that leaves a case-only twin,
invalidates it". A proof that has to be repeated by hand is a proof that will not
be; this file is that audit's replacement.

**Why it cannot be done with `is_file()`.** On Windows ``Path("src/forgepilot/runtime/Events.py").is_file()``
answers ``True``. Ask the filesystem whether a case-wrong path exists and it says
yes; ask it whether the import will work and it says yes about a file CPython
cannot find on Linux. So every check below compares **exact strings** against the
set of paths actually on disk (:data:`EXACT`). That is also why this test lives in
`tests/contract/` rather than behind a platform marker: on Windows it is the only
thing that can see the mistake, and on Linux it is the same assertion, failing
earlier and louder.

Deliberately *not* done here: nothing is renamed (there is nothing to rename —
measured 0 violations), and no `ruff` rule is written for it. The check is about
paths and their spelling, which is what `ast` plus this module's own walk already
give; a linter plugin would be a second way to say the same thing, and the ways
would drift.

Every check reports **all** offenders, not the first: a rename that breaks twelve
imports should be one run away from fixed, not twelve.
"""

from __future__ import annotations

import ast
import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The trees whose contents are imported by something. A path under one of these
#: is a module path; a path outside them (``docs/``, ``evals/results/``) is prose
#: or a product, and whether it has a capital is not a question about imports.
TREES = ("src", "tests", "tools", "evals", "examples")
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".venv-linux",
        ".forgepilot",
        ".tmp",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "graphify-out",
        "node_modules",
    }
)
#: ``plugin: package.module:ClassName`` — the one place a module path is spelled
#: outside Python. Two patterns, because a row that is *not* of that shape is a
#: row that will fail at boot: ``resolve_plugin`` refuses any spec without a colon.
_PLUGIN_ROW = re.compile(r"""^\s*plugin:\s*(?P<value>.*?)\s*$""")
_PLUGIN_SPEC = re.compile(r"""^["']?(?P<module>[A-Za-z_][\w.]*):(?P<attr>[A-Za-z_]\w*)["']?$""")


def _repo_files() -> tuple[Path, ...]:
    """Every file in the repository, as a path relative to its root."""
    found: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT)
        if IGNORED_DIRS.intersection(rel.parts):
            continue
        if path.is_file():
            found.append(rel)
    return tuple(sorted(found))


def _tree_files() -> tuple[Path, ...]:
    return tuple(rel for rel in _repo_files() if rel.parts and rel.parts[0] in TREES)


#: Every file under the five trees, and the exact spellings an import must match.
FILES = _tree_files()
EXACT = frozenset(rel.as_posix() for rel in FILES)
PY_FILES = tuple(rel for rel in FILES if rel.suffix == ".py")
YAML_FILES = tuple(rel for rel in _repo_files() if rel.suffix in {".yaml", ".yml"})

#: One path from each tree that no checkout can be missing, as the exact `EXACT` spelling.
#: Presence is what the vacuity guard asserts, so the guard cannot depend on how much
#: untracked output happens to be lying around.
TREE_ANCHORS = (
    "src/forgepilot/__init__.py",
    "tests/conftest.py",
    "tools/check.py",
    "evals/run.py",
    "examples/stat-fix/fixture/stats.py",
)


def _module_path(dotted: str) -> Path | None:
    """The file :data:`EXACT` spells for ``dotted``, or ``None``.

    A dotted name is resolved in two steps because the tree root is not always
    part of it: ``forgepilot.seams.retriever`` lives under ``src/``, while
    ``harness`` and ``run`` are imported bare from ``evals/``. Trying the spelling
    under each tree covers both without a table of special cases — a table would be
    one more thing to update when the next script imports its neighbour.
    """
    stem = dotted.replace(".", "/")
    for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
        if candidate in EXACT:
            return Path(candidate)
    for tree in TREES:
        for candidate in (f"{tree}/{stem}.py", f"{tree}/{stem}/__init__.py"):
            if candidate in EXACT:
                return Path(candidate)
    return None


def _case_folded_sibling(wanted: str) -> str | None:
    """The on-disk spelling of ``wanted`` if only its case differs, else ``None``.

    ``wanted`` is a path with or without its tree prefix, so the match is on the
    folded *tail*: an import spelled ``forgepilot.Seams.refs`` must be told about
    ``src/forgepilot/seams/refs.py``, which is where the repository actually keeps
    it, even though the import never mentions ``src``.
    """
    if wanted in EXACT:
        return wanted
    folded = wanted.lower()
    for rel in sorted(EXACT):
        if rel.lower() == folded or rel.lower().endswith("/" + folded):
            return rel
    return None


@dataclass(frozen=True)
class Reference:
    """One module an import statement names, and where it was written."""

    path: Path
    line: int
    dotted: str
    names: tuple[str, ...] = ()

    def spelled(self) -> str:
        return f"{self.path.as_posix()}:{self.line} imports {self.dotted!r}"


def _assigned(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Tuple | ast.List):
        return {name for element in target.elts for name in _assigned(element)}
    return set()


@cache
def _bound_names(rel: Path) -> frozenset[str]:
    """Module-level names ``rel`` binds — what ``from rel import name`` needs.

    Only module-level statements count, including the bodies of ``if``/``try``/
    ``with`` at module level (a conditional import still binds if the branch runs).
    A name assigned inside a function is deliberately *not* counted: ``def f():
    Foo = 1`` does not make ``from mod import Foo`` legal, and a check that
    accepted it would be quiet about a real ImportError.

    ``__all__`` strings are deliberately not counted either. ``__all__`` lists what
    a module *exports*, not what it *binds*; a module that names a symbol in
    ``__all__`` without importing or defining it is broken on its own, and
    accepting it here would hide exactly the mistake this file exists to catch.
    """
    tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"), filename=str(rel))
    names: set[str] = set()
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(name for target in node.targets for name in _assigned(target))
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            names.update(_assigned(node.target))
        elif isinstance(node, ast.If | ast.Try | ast.TryStar | ast.With | ast.AsyncWith):
            stack.extend(getattr(node, "body", ()))
            stack.extend(getattr(node, "orelse", ()))
            stack.extend(getattr(node, "finalbody", ()))
            for handler in getattr(node, "handlers", ()):
                stack.extend(handler.body)
    return frozenset(names)


def _in_scope(dotted: str) -> bool:
    """Whether ``dotted`` is this project's to answer for.

    A reference is in scope when its head is a tree name (``forgepilot``,
    ``tests`` …) or when some file ``<tree>/<head>.py`` exists — which is how the
    bare ``import harness`` in ``evals/`` and the fixture's ``import stats`` are
    recognised. `json`, `pathlib` and a third-party `yaml` match neither and are
    left to the interpreter. Self-maintaining on purpose: a new sibling script is
    covered the day it is written, and a name that is *not* covered is a name no
    import in this tree claims.
    """
    head = dotted.split(".")[0]
    if head in TREES or head == "forgepilot":
        return True
    return any(f"{tree}/{head}.py" in EXACT for tree in TREES)


def _references() -> tuple[Reference, ...]:
    found: list[Reference] = []
    for rel in PY_FILES:
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _in_scope(alias.name):
                        found.append(Reference(rel, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue  # reported by test_no_import_is_relative
                if node.module and _in_scope(node.module):
                    found.append(
                        Reference(rel, node.lineno, node.module, tuple(a.name for a in node.names))
                    )
    return tuple(found)


REFERENCES = _references()


def _resolution_problems() -> list[str]:
    """Every reference that would not import on a case-sensitive filesystem."""
    problems: list[str] = []
    for reference in REFERENCES:
        module = _module_path(reference.dotted)
        if module is None:
            sibling = _case_folded_sibling(reference.dotted.replace(".", "/") + ".py")
            hint = (
                f" — did you mean {sibling!r}? The spelling above differs only by case, so it "
                f"works on this filesystem and fails on Linux."
                if sibling
                else " — no file under src/, tests/, tools/, evals/ or examples/ spells it."
            )
            problems.append(f"{reference.spelled()}{hint}")
            continue
        bound = _bound_names(module)
        for name in reference.names:
            if name == "*":
                continue
            submodule = f"{module.parent.as_posix()}/{name}".lstrip("./")
            if f"{submodule}.py" in EXACT or f"{submodule}/__init__.py" in EXACT:
                continue  # `from package import submodule`
            if name in bound:
                continue  # `from module import Symbol`
            sibling = _case_folded_sibling(f"{submodule}.py")
            hint = (
                f" — {module.as_posix()} does not bind it, but {sibling!r} exists and differs "
                f"only by case."
                if sibling
                else f" — {module.as_posix()} neither contains it as a submodule nor binds it."
            )
            problems.append(f"{reference.spelled()} name {name!r}{hint}")
    return problems


def test_every_intra_project_import_is_spelled_exactly_as_it_is_on_disk() -> None:
    problems = _resolution_problems()
    assert not problems, (
        "these imports name a module or a symbol that this repository does not spell that "
        "way:\n  " + "\n  ".join(problems) + "\n\nOn Windows this import works; on Linux it "
        "raises ImportError, and CI runs on Linux. Fix the spelling rather than adding an "
        "exemption — there is deliberately none."
    )


def test_no_path_component_uses_a_capital() -> None:
    offenders = [
        rel.as_posix()
        for rel in PY_FILES
        if any(char.isupper() for part in rel.parts for char in part)
    ]
    assert not offenders, (
        "these Python files live at a path containing a capital letter, so importing them as "
        "written depends on the filesystem folding case:\n  " + "\n  ".join(offenders)
    )


def _case_only_twins(files: tuple[Path, ...]) -> list[str]:
    groups: dict[str, list[str]] = {}
    for rel in files:
        groups.setdefault(rel.as_posix().lower(), []).append(rel.as_posix())
    return [
        " and ".join(sorted(spellings)) for spellings in groups.values() if len(set(spellings)) > 1
    ]


def test_no_two_entries_differ_only_by_case() -> None:
    """A rename that leaves a case-only twin is invisible here and fatal on Linux."""
    offenders = _case_only_twins(FILES)
    assert not offenders, (
        "these entries differ only by case, so only one of them exists on a case-insensitive "
        "filesystem and `git checkout` on Linux produces both:\n  " + "\n  ".join(offenders)
    )


def test_the_twin_check_sees_a_twin_this_filesystem_cannot_hold() -> None:
    """The one check that cannot be falsified by creating the condition on this box.

    Two files differing only by case cannot coexist on Windows, so the rule is
    exercised on a constructed pair instead. Without this the check would be the
    only one in the file with no evidence behind it — and the failure it looks for
    is the one that only appears after a `git checkout` on Linux.
    """
    twins = _case_only_twins((Path("src/sample.py"), Path("src/Sample.py")))
    assert twins == ["src/Sample.py and src/sample.py"], twins
    assert _case_only_twins((Path("src/sample.py"), Path("tests/sample.py"))) == []


def test_no_import_is_relative() -> None:
    offenders: list[str] = []
    for rel in PY_FILES:
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                dots = "." * node.level
                offenders.append(
                    f"{rel.as_posix()}:{node.lineno} `from {dots}{node.module or ''} import ...`"
                )
    assert not offenders, (
        "these imports are relative, so which file they mean depends on where the importer "
        "lives and on the package path the runner assembled:\n  " + "\n  ".join(offenders)
    )


def test_every_plugin_row_names_a_module_and_a_class_that_exist() -> None:
    """The configuration plane spells module paths too, and nothing checks them.

    A row is resolved by name when the profile is loaded, so a wrong spelling — or
    a value that is not a ``module:Attribute`` spec at all — fails at boot rather
    than where it was written. The rows are read as text rather than through a YAML
    parser on purpose: the comment form has to be skipped, and a parser would also
    silently accept a value this check exists to reject.
    """
    problems: list[str] = []
    for rel in YAML_FILES:
        lines = (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()
        for lineno, raw in enumerate(lines, start=1):
            if raw.lstrip().startswith("#"):
                continue  # a commented-out or illustrative row names no real plugin
            match = _PLUGIN_ROW.match(raw)
            if match is None:
                continue
            value = match.group("value").split(" #")[0].strip()
            if value in {"", "null", "~"}:
                continue  # an entry may inherit `plugin` from an earlier layer
            spec = _PLUGIN_SPEC.match(value)
            if spec is None:
                problems.append(
                    f"{rel.as_posix()}:{lineno} has plugin {value!r}, which is not a "
                    f"'package.module:Attribute' spec"
                )
                continue
            dotted, class_name = spec.group("module"), spec.group("attr")
            module = _module_path(dotted)
            if module is None:
                sibling = _case_folded_sibling(dotted.replace(".", "/") + ".py")
                hint = f" — did you mean {sibling!r}?" if sibling else ""
                problems.append(
                    f"{rel.as_posix()}:{lineno} names module {dotted!r}, which does not exist{hint}"
                )
            elif class_name not in _bound_names(module):
                problems.append(
                    f"{rel.as_posix()}:{lineno} names {dotted}:{class_name}, but "
                    f"{module.as_posix()} does not bind {class_name!r}"
                )
    assert not problems, (
        "these plugin rows would fail to load — the row is resolved by name at boot, so the "
        "failure appears when the profile is used rather than when it is written:\n  "
        + "\n  ".join(problems)
    )


def test_the_pytest_configuration_cannot_hide_a_case_mismatch() -> None:
    """A `pythonpath` entry or a second config file can make a wrong spelling work."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ini = pyproject["tool"]["pytest"]["ini_options"]
    assert ini["testpaths"] == ["tests"], (
        f"pytest collects {ini.get('testpaths')!r}; the case checks in this file assume the "
        f"repository is on sys.path as it is on disk, and that is decided here"
    )
    assert "pythonpath" not in ini, (
        "`pythonpath` adds a directory to sys.path by name, which is exactly the indirection "
        "that lets a case-wrong import work on Windows and fail on Linux"
    )
    shadowing = [
        name for name in ("pytest.ini", "setup.cfg", "tox.ini") if (REPO_ROOT / name).exists()
    ]
    assert not shadowing, (
        f"{shadowing} would take precedence over pyproject.toml, so the assertions above would "
        f"be reading a file pytest ignores"
    )


def test_the_walk_actually_saw_the_tree() -> None:
    """Guard against every check above passing on an empty or renamed walk.

    This file's whole value is a negative result — "no spelling is wrong" — and a
    negative result from a walk that found nothing looks identical. So the guard
    asserts **presence**, one path per tree, rather than a size.

    A total-file bound was tried first and CI refuted it: this box walks 442 files
    while a fresh checkout walks **347**, because the difference is untracked,
    gitignored output under `evals/results/`. A threshold calibrated on the
    developer's dirty tree is the same mistake as a path calibrated on the
    developer's filesystem, one level up.
    """
    assert len(PY_FILES) > 100, f"expected many Python files, found {len(PY_FILES)}"
    assert len(REFERENCES) > 500, f"expected many intra-project imports, found {len(REFERENCES)}"
    for anchor in TREE_ANCHORS:
        assert anchor in EXACT, f"the walk did not see {anchor!r}; this is not the five trees"
