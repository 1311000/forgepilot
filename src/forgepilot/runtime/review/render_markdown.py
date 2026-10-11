"""Rendering the narrative as Markdown.

Markdown rather than a bespoke format because it lands where reviewers already
are: a PR description, a terminal, a chat message. Research on review tooling is
consistent that workflow fit decides adoption more than analytical depth, and a
beautiful review nobody opens is worth less than a plain one that shows up in the
thread.

Two sections carry the weight:

**覆盖情况** states how many hunks *no claim cited*, and names them. This is the
answer to the finding that a change with many comments can still be unevenly
reviewed — comment volume looks like assurance, and a tool that cannot say "I did
not examine this" cannot be trusted when it implies it examined everything.

**未能核实** is a first-class section, not an error report. Its length is a
signal: the longer it is, the more this change needs a human rather than less.
"""

from __future__ import annotations

from forgepilot.seams.review import (
    ChangeNarrative,
    ChangeRole,
    Claim,
    DiffLineKind,
)

__all__ = ["render_markdown"]

#: Diff lines shown per hunk before eliding. Enough to see the change's shape,
#: short enough that a big hunk does not push the narrative off the page.
_MAX_LINES_PER_HUNK = 24


def _cite(claim: Claim) -> str:
    if not claim.evidence:
        return ""
    shown = ", ".join(f"`{ref.describe()}`" for ref in claim.evidence[:4])
    extra = f" +{len(claim.evidence) - 4}" if len(claim.evidence) > 4 else ""
    return f"  `[证据 → {shown}{extra}]`"


def _render_hunk(hunk) -> list[str]:
    lines = ["```diff"]
    shown = hunk.lines[:_MAX_LINES_PER_HUNK]
    if hunk.section:
        lines.append(f"@@ {hunk.section}")
    for line in shown:
        prefix = {
            DiffLineKind.ADD: "+",
            DiffLineKind.REMOVE: "-",
            DiffLineKind.CONTEXT: " ",
        }[line.kind]
        lines.append(f"{prefix}{line.text}")
    hidden = len(hunk.lines) - len(shown)
    if hidden > 0:
        lines.append(f"… 还有 {hidden} 行")
    lines.append("```")
    return lines


def render_markdown(narrative: ChangeNarrative) -> str:
    out: list[str] = []

    if narrative.title:
        out.append(f"# {narrative.title}")
    header_bits = [narrative.summary_line] if narrative.summary_line else []
    if narrative.session_id:
        header_bits.append(f"会话 `{narrative.session_id}`")
    if narrative.evidence:
        header_bits.append(f"{len(narrative.evidence)} 条证据")
    backend = narrative.blast_radius
    header_bits.append(
        f"影响面分析: {'AST' if backend.is_ast else '文本匹配'}"
    )
    if header_bits:
        out.append("")
        out.append("> " + " · ".join(header_bits))

    # -- intent ------------------------------------------------------------
    if narrative.intent.text:
        out.append("")
        out.append("## 意图")
        out.append(narrative.intent.text + _cite(narrative.intent))

    # -- approach ----------------------------------------------------------
    if narrative.approach:
        out.append("")
        out.append("## 思路")
        for index, claim in enumerate(narrative.approach, start=1):
            out.append(f"{index}. {claim.text}{_cite(claim)}")

    # -- reading order -----------------------------------------------------
    substantive = [g for g in narrative.groups if g.role is not ChangeRole.NOISE]
    noise = [g for g in narrative.groups if g.role is ChangeRole.NOISE]

    if substantive:
        out.append("")
        out.append("## 建议的阅读顺序")
        out.append("")
        out.append(
            "> 顺序是按「读它之前需要先知道什么」排的，不是按文件名。"
            "字母序会把最好的注意力花在恰好排在前面的事情上。"
        )
        for index, group in enumerate(substantive, start=1):
            paths = group.paths
            first = " ← 从这里开始" if index == 1 else ""
            out.append("")
            out.append(f"### {index}｜{group.title} · {', '.join(f'`{p}`' for p in paths[:3])}{first}")
            out.append("")
            out.append(f"> {group.rationale.text}{_cite(group.rationale)}")
            if group.position_reason:
                out.append("> ")
                out.append(f"> *为什么在这里：{group.position_reason}*")
            for hunk in group.hunks[:2]:
                out.append("")
                out.extend(_render_hunk(hunk))
            if len(group.hunks) > 2:
                out.append("")
                out.append(f"*（本组还有 {len(group.hunks) - 2} 个块）*")

    if noise:
        total = sum(len(g.hunks) for g in noise)
        paths = [p for g in noise for p in g.paths]
        out.append("")
        out.append(f"<details><summary>噪声（{total} 个块，{len(paths)} 个文件，已折叠）</summary>")
        out.append("")
        for path in paths[:12]:
            out.append(f"- `{path}`")
        out.append("")
        out.append(
            "默认折叠，但它们没有被删掉——改了锁定文件本身可能就是个信号"
            "（依赖动了）。"
        )
        out.append("</details>")

    # -- blast radius ------------------------------------------------------
    if backend.symbols or backend.affected_callers:
        out.append("")
        out.append(f"## 影响范围（{'AST 分析' if backend.is_ast else '文本匹配'}）")
        if backend.symbols:
            for change in backend.symbols[:12]:
                out.append(f"- `{change.name}` — {change.kind.value}（`{change.path}`）")
        if backend.affected_callers:
            out.append(f"- 受影响的调用点 **{len(backend.affected_callers)} 处**：")
            for ref in backend.affected_callers[:12]:
                out.append(f"  `{ref.path}`")
        if backend.note:
            out.append("")
            out.append(f"> ⚠ {backend.note}")
    elif backend.truncated:
        out.append("")
        out.append("## 影响范围")
        out.append("> 结果被截断，未列出的调用点仍然存在。")

    # -- coverage ----------------------------------------------------------
    coverage = narrative.coverage
    out.append("")
    out.append("## 覆盖情况")
    out.append("")
    out.append(
        f"块 {coverage.total_hunks} · 有论断 {coverage.annotated_hunks} · "
        f"噪声 {coverage.noise_hunks} · **未审视 {coverage.unexamined_hunks}**"
    )
    if coverage.unexamined_hunks:
        out.append("")
        out.append(
            f"⚠ 有 {coverage.unexamined_hunks} 个块**没有任何论断**，"
            f"工具没有看过它们："
        )
        for path in coverage.unexamined[:10]:
            out.append(f"- `{path}`")
        out.append("")
        out.append(
            "这些不是「没问题」，是「没人看」。评论数量不等于覆盖均匀——"
            "这一行就是为了让这件事没法被忽略。"
        )
    else:
        out.append("")
        out.append("每一个非噪声的块都有论断，并被证据引用。")
    if not coverage.tests_changed:
        out.append("")
        out.append("⚠ 本次改动不包含任何测试文件。")

    # -- unverified --------------------------------------------------------
    out.append("")
    out.append("## 未能核实")
    if narrative.unverified:
        out.append("")
        out.append(
            "以下论断**没有通过校验**，所以它们不算「已确认」。"
            "这一节越长，说明这次改动越需要人来看。"
        )
        out.append("")
        for claim in narrative.unverified:
            reason = claim.unverified_reason or "no evidence"
            out.append(f"- {claim.text}")
            out.append(f"  - 原因：{reason}")
    else:
        out.append("")
        out.append("所有论断都通过了校验。")

    # -- evidence appendix -------------------------------------------------
    if narrative.evidence:
        out.append("")
        out.append(f"<details><summary>全部证据（{len(narrative.evidence)} 条）</summary>")
        out.append("")
        for ref in narrative.evidence[:80]:
            out.append(f"- `{ref.kind.value}` {ref.describe()}  {ref.detail}")
        out.append("</details>")

    return "\n".join(out) + "\n"
