"""Putting the change in reading order.

**This module is where the review stops being a summariser.**

Research on code review is consistent that reviewers read a file list top-down
and their attention decays: bugs in the first file are markedly more likely to be
found than in the last, and a file's position predicts how much of it gets
looked at. An alphabetical listing therefore spends the reviewer's best attention
on whatever happens to sort first — which is to say, on luck.

So the order here is chosen, and chosen to put *information before it is needed*:

    schema -> logic -> api -> config -> tests -> docs -> noise

Read the shape of the data, then the logic that uses it, then the code that calls
that logic, then configuration, then the tests that confirm it. Within a role:
things other changed files depend on come first, then bigger changes before
smaller ones — the same principle again, spending scarce early attention on what
matters most.

Each group also carries :attr:`ChangeGroup.position_reason`, so a reviewer can
see *why* it is here rather than having to trust the tool's ordering. Research on
review tooling is explicit that ordering the reviewer cannot explain erodes
their confidence in it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from forgepilot.runtime.review.diffmodel import ParsedDiff, ParsedFile
from forgepilot.seams.review import (
    ROLE_ORDER,
    ROLE_TITLES,
    ChangedHunk,
    ChangeGroup,
    ChangeRole,
    Claim,
    EvidenceKind,
    EvidenceRef,
)

__all__ = ["assign_role", "group_hunks", "order_groups", "position_reason"]


_TESTS = re.compile(r"(^|/)(tests?|__tests__|specs?)/|(^|/)test_|_test\.|\.test\.|\.spec\.")
_DOCS = re.compile(r"\.(md|rst|txt|adoc)$|(^|/)docs?/")
_CONFIG = re.compile(
    r"\.(ya?ml|toml|ini|cfg|conf|properties)$|(^|/)(dockerfile|makefile)$|(^|/)\.env"
)
_API = re.compile(r"(^|/)(api|routes?|views?|handlers?|controllers?|endpoints?)/|(^|/)__init__\.py$")
_SCHEMA = re.compile(r"(^|/)(migrations?|schema|models|entities)/|\.sql$|(^|/)schema\.")

#: Files with these suffixes are treated as code for the purpose of role
#: assignment; anything else falls through to LOGIC.
_CODE_SUFFIXES = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
    ".rb", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".php",
)


def assign_role(path: str, *, noise: bool = False) -> ChangeRole:
    """Decide what kind of file this is.

    Order of checks matters: a test named `schema_test.py` is a test, not a
    schema. The most specific signal wins.
    """
    if noise:
        return ChangeRole.NOISE

    lowered = path.lower()
    if _TESTS.search(lowered):
        return ChangeRole.TESTS
    if _DOCS.search(lowered):
        return ChangeRole.DOCS
    if _SCHEMA.search(lowered):
        return ChangeRole.SCHEMA
    if _CONFIG.search(lowered):
        return ChangeRole.CONFIG
    if _API.search(lowered):
        return ChangeRole.API
    return ChangeRole.LOGIC


def _referenced_by(
    path: str, other_paths: tuple[str, ...], sources: Mapping[str, str]
) -> bool:
    """Whether any *other* changed file appears to reference ``path``.

    Text-level on purpose, and it is a heuristic. It errs toward saying "referenced"
    when a module name merely appears somewhere, because the cost of a wrong
    ordering is small and the cost of a missing dependency edge is that the
    reviewer meets a file before the one it depends on.
    """
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if not stem or stem in {"__init__", "index", "main"}:
        return False
    pattern = re.compile(rf"\b{re.escape(stem)}\b")
    for other in other_paths:
        if other == path:
            continue
        body = sources.get(other)
        if body and pattern.search(body):
            return True
    return False


def group_hunks(
    diff: ParsedDiff, *, sources: Mapping[str, str] | None = None
) -> tuple[ChangeGroup, ...]:
    """Group hunks by role and order them for reading.

    ``sources`` is an optional map of changed path to file contents, used only to
    order files within a role by dependency. Without it the order falls back to
    churn, which is the more important of the two rules anyway.
    """
    sources = sources or {}
    by_role: dict[ChangeRole, list[ParsedFile]] = {}

    for parsed in diff.files:
        role = assign_role(parsed.path, noise=parsed.noise is not None)
        by_role.setdefault(role, []).append(parsed)

    groups: list[ChangeGroup] = []
    for role in ROLE_ORDER:
        files = by_role.get(role)
        if not files:
            continue

        paths = tuple(f.path for f in files)
        files.sort(
            key=lambda f: (
                # Referenced-by others first: read a file before the things that
                # use it.
                0 if sources and _referenced_by(f.path, paths, sources) else 1,
                # Then bigger changes first, while attention is freshest.
                -f.churn,
                f.path,
            )
        )

        hunks = tuple(h for f in files for h in f.hunks)
        if not hunks:
            continue

        groups.append(
            ChangeGroup(
                title=ROLE_TITLES.get(role, str(role)),
                role=role,
                rationale=_deterministic_rationale(role, files, hunks),
                hunks=hunks,
                order=ROLE_ORDER.index(role),
                position_reason=position_reason(role, files),
            )
        )

    return tuple(groups)


def _deterministic_rationale(
    role: ChangeRole, files: list[ParsedFile], hunks: tuple[ChangedHunk, ...]
) -> Claim:
    """A description of the group that is *itself* evidenced, before any model runs.

    Built from the diff, so it is verifiable by construction. A model may replace
    it with something more informative, but only if it brings evidence with it —
    and if it does not, this is what the reader sees.
    """
    added = sum(len(h.added) for h in hunks)
    removed = sum(len(h.removed) for h in hunks)
    names = ", ".join(f"`{f.path}`" for f in files[:4])
    more = f" 等 {len(files)} 个文件" if len(files) > 4 else ""
    descriptions = {
        ChangeRole.SCHEMA: "数据形状的改动，先读它才能理解后面用到它的逻辑。",
        ChangeRole.LOGIC: "核心逻辑改动。",
        ChangeRole.API: "对外接口或调用点，放在逻辑之后读，因为它依赖前面定义的形状。",
        ChangeRole.CONFIG: "配置改动，影响上面逻辑运行时的取值。",
        ChangeRole.TESTS: "测试，放在最后确认前面的改动真的被断言了。",
        ChangeRole.DOCS: "文档。",
        ChangeRole.NOISE: "依赖锁定与生成物，默认折叠。",
    }
    return Claim(
        text=f"{names}{more}：+{added} −{removed}。{descriptions.get(role, '')}",
        evidence=tuple(
            EvidenceRef(
                kind=EvidenceKind.DIFF_HUNK,
                hunk_id=hunk.hunk_id,
                detail=f"{hunk.path} +{len(hunk.added)} −{len(hunk.removed)}",
            )
            for hunk in hunks[:20]
        ),
    )


def position_reason(role: ChangeRole, files: list[ParsedFile]) -> str:
    """Why this group sits where it does — stated, not implied."""
    if role is ChangeRole.SCHEMA:
        return "先读：后面所有逻辑都建立在这个数据形状上。"
    if role is ChangeRole.LOGIC:
        return (
            "从这里开始：改动最集中、最需要判断的地方。"
            if ROLE_ORDER.index(role) <= 1
            else "核心逻辑。"
        )
    if role is ChangeRole.API:
        return "在逻辑之后：这里的调用点依赖前面定义的接口。"
    if role is ChangeRole.CONFIG:
        return "配置：影响上面的逻辑在运行时取到什么值。"
    if role is ChangeRole.TESTS:
        return "最后确认：看它断言了什么，而不只是看它通过了。"
    if role is ChangeRole.DOCS:
        return "文档，可以在代码之后再看。"
    return "默认折叠，因为它们很少值得占用注意力——但计数保留，改了锁定文件本身就是个信号。"


def order_groups(groups: tuple[ChangeGroup, ...]) -> tuple[ChangeGroup, ...]:
    """Apply :data:`ROLE_ORDER`. Kept separate so it can be tested on its own."""
    return tuple(sorted(groups, key=lambda g: g.order))
