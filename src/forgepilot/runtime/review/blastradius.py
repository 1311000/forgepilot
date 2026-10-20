"""What else does this change touch?

Two backends, and **the result says which one produced it**.

- **AST** (tree-sitter): real definitions, real identifier references. Knowing
  that a *signature* changed rather than a body, and finding every call site, is
  the difference between "this function changed" and "these three places will
  break".
- **Text**: word-boundary search for the changed names, plus a regex for
  definitions. Cheaper, needs nothing installed, and **has false positives** —
  a comment mentioning the name, or a different symbol that happens to share it.

⚠️ **Why this matters here.** `tree-sitter` publishes no cp314 wheel for Windows
(win_amd64 stops at cp313; cp314 exists only for macOS and manylinux) and has no
abi3 or pure-Python fallback, so it cannot be installed on this development
machine without a C toolchain. Production is Linux, where the wheel exists.

So the backend is probed at runtime, and :attr:`BlastRadius.backend` records what
was actually used. A reader who is told "3 call sites" deserves to know whether
that came from parsing or from grep — the same principle as the recording sandbox
that had to admit it never executed anything. Reporting a text search as an
impact analysis would be the same lie in a different place.

⚠️ **The AST path is untested in this repository's development environment.** It
cannot be: tree-sitter will not install on Windows + Python 3.14. Its tests skip
with a reason naming that, and the code below is written against the tree-sitter
0.25+ API (`Parser(language)`) without having been executed. Treat it as
reviewed-but-unrun until someone runs it on Linux — the same standard applied to
the DeepSeek provider's live call in M6.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from forgepilot.seams.refs import CodeRef, RepoRef
from forgepilot.seams.review import (
    AnalysisBackend,
    BlastRadius,
    SymbolChange,
    SymbolChangeKind,
)

__all__ = [
    "SymbolInfo",
    "analysis_backend",
    "ast_available",
    "extract_symbols",
    "find_references",
    "analyse_blast_radius",
]

#: Definitions, for the text backend. Covers the languages in the fixtures; a
#: real deployment would lean on the AST backend and not on this.
_DEF_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>\w+)\s*(?P<sig>\([^)]*)"),
    re.compile(r"^\s*class\s+(?P<name>\w+)\s*(?P<sig>[:(])"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>\w+)\s*(?P<sig>\([^)]*)"),
    re.compile(
        r"^\s*(?:public|private|protected|static|final|\s)*"
        r"(?P<name>\w+)\s*(?P<sig>\([^)]*\))\s*(?::\s*\w+)?\s*\{"
    ),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*=\s*(?:async\s*)?\("),
)

_MAX_REFERENCES = 200


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    name: str
    kind: str
    path: str
    line: int
    signature: str = ""


def ast_available() -> bool:
    """Whether tree-sitter's runtime can be imported.

    A probe rather than a config flag: whether it imports is the only thing that
    actually determines which analysis we can run.
    """
    try:
        import tree_sitter  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "not available"
        return False
    return True


def analysis_backend() -> AnalysisBackend:
    """Which backend will be used. Reported in the output, never assumed."""
    return AnalysisBackend.AST if ast_available() else AnalysisBackend.TEXT


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------
def _extract_text(source: str, path: str) -> tuple[SymbolInfo, ...]:
    found: list[SymbolInfo] = []
    for number, line in enumerate(source.splitlines(), start=1):
        for pattern in _DEF_PATTERNS:
            match = pattern.match(line)
            if match:
                found.append(
                    SymbolInfo(
                        name=match.group("name"),
                        kind="definition",
                        path=path,
                        line=number,
                        signature=(match.group("sig") or "").strip("(: "),
                    )
                )
                break
    return tuple(found)


def _extract_ast(source: str, path: str) -> tuple[SymbolInfo, ...]:
    """Definitions via tree-sitter, for Python.

    Falls back to the text extractor for languages with no grammar installed,
    and says so through the return value being the text one — callers only ever
    see `BlastRadius.backend`, which is computed once.
    """
    from tree_sitter import Language, Parser  # noqa: PLC0415

    try:
        import tree_sitter_python as tspython  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return _extract_text(source, path)

    language = Language(tspython.language())
    parser = Parser(language)
    tree = parser.parse(source.encode("utf-8"))

    found: list[SymbolInfo] = []

    def walk(node) -> None:
        if node.type in {"function_definition", "class_definition"}:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                found.append(
                    SymbolInfo(
                        name=source.encode("utf-8")[
                            name_node.start_byte : name_node.end_byte
                        ].decode("utf-8", errors="replace"),
                        kind="definition",
                        path=path,
                        line=name_node.start_point[0] + 1,
                    )
                )
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return tuple(found)


def extract_symbols(
    source: str, path: str, *, backend: AnalysisBackend | None = None
) -> tuple[SymbolInfo, ...]:
    chosen = backend or analysis_backend()
    return (
        _extract_ast(source, path) if chosen is AnalysisBackend.AST else _extract_text(source, path)
    )


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------
def _references_text(name: str, source: str) -> tuple[int, ...]:
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    return tuple(
        number
        for number, line in enumerate(source.splitlines(), start=1)
        # A line that is only a definition of the name is not a *use* of it, and
        # counting it would make every definition look like its own caller.
        if pattern.search(line) and not any(p.match(line) for p in _DEF_PATTERNS)
    )


def _references_ast(name: str, source: str) -> tuple[int, ...]:
    from tree_sitter import Language, Parser  # noqa: PLC0415

    try:
        import tree_sitter_python as tspython  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return _references_text(name, source)

    parser = Parser(Language(tspython.language()))
    tree = parser.parse(source.encode("utf-8"))
    target = name.encode("utf-8")
    lines: list[int] = []

    def walk(node) -> None:
        if (
            node.type == "identifier"
            and source.encode("utf-8")[node.start_byte : node.end_byte] == target
        ):
            lines.append(node.start_point[0] + 1)
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return tuple(sorted(set(lines)))


def find_references(
    sources: Mapping[str, str],
    names: Sequence[str],
    *,
    repo: RepoRef,
    ref: str,
    backend: AnalysisBackend | None = None,
) -> tuple[CodeRef, ...]:
    """Locate uses of ``names`` across ``sources``, as citable locations."""
    chosen = backend or analysis_backend()
    seen: set[tuple[str, int]] = set()
    refs: list[CodeRef] = []

    for path, source in sources.items():
        per_file: list[int] = []
        for name in names:
            per_file.extend(
                _references_ast(name, source)
                if chosen is AnalysisBackend.AST
                else _references_text(name, source)
            )
        for line in sorted(set(per_file)):
            if (path, line) in seen:
                continue
            seen.add((path, line))
            refs.append(CodeRef(repo=repo, path=path, ref=ref))
            if len(refs) >= _MAX_REFERENCES:
                return tuple(refs)
    return tuple(refs)


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------
def analyse_blast_radius(
    *,
    changed_sources: Mapping[str, str],
    removed_lines: Sequence[str],
    workspace_sources: Mapping[str, str] | None = None,
    repo: RepoRef | None = None,
    ref: str = "HEAD",
) -> BlastRadius:
    """Compare the symbols the diff added against those it removed, then find uses.

    ``changed_sources`` is the current content of the changed files;
    ``removed_lines`` is everything the diff took away. There is no "old file" to
    parse, so what disappeared is inferred from the removed lines — which is why
    a *signature* change is detected by comparing the signature text of a name
    present on both sides, not by a structural match.
    """
    backend = analysis_backend()
    current: list[SymbolInfo] = []
    for path, source in changed_sources.items():
        current.extend(extract_symbols(source, path, backend=backend))

    previous = _extract_text("\n".join(removed_lines), "<removed>")
    now = {info.name: info for info in current}
    before = {info.name: info for info in previous}

    changes: list[SymbolChange] = []
    for name, info in now.items():
        if name not in before:
            changes.append(SymbolChange(name=name, kind=SymbolChangeKind.ADDED, path=info.path))
        elif before[name].signature and info.signature and before[name].signature != info.signature:
            changes.append(
                SymbolChange(name=name, kind=SymbolChangeKind.SIGNATURE_CHANGED, path=info.path)
            )
    for name, info in before.items():
        if name not in now:
            changes.append(SymbolChange(name=name, kind=SymbolChangeKind.REMOVED, path=info.path))

    searchable = dict(workspace_sources or {})
    candidates = tuple(
        sorted(
            {
                c.name
                for c in changes
                if c.kind in {SymbolChangeKind.REMOVED, SymbolChangeKind.SIGNATURE_CHANGED}
            }
        )
    )
    # Only symbols that *changed shape* are interesting for references. An added
    # function has no existing callers by definition, and searching for it would
    # pad the report with noise.
    affected = (
        find_references(
            searchable,
            candidates,
            repo=repo or RepoRef(host="", namespace="", name=""),
            ref=ref,
            backend=backend,
        )
        if candidates
        else ()
    )
    truncated = len(affected) >= _MAX_REFERENCES

    note = ""
    if backend is AnalysisBackend.TEXT:
        note = (
            "文本匹配：按名字做词边界搜索，可能有同名符号造成的假阳性。"
            "装上 tree-sitter（需要 Linux，见 CLAUDE.md）即为 AST 分析。"
        )
    if truncated:
        note = (note + " " if note else "") + "结果已截断，未列出的调用点仍然存在。"

    return BlastRadius(
        backend=backend,
        symbols=tuple(changes),
        affected_callers=tuple(affected),
        truncated=truncated,
        note=note,
    )
