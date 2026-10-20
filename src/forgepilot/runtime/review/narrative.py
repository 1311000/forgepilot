"""Assembling the narrative, and counting what it did not cover.

The model may write the prose, but it does not get to decide what counts as
evidence. Two paths:

- :func:`deterministic_narrative` builds claims **from the diff**, so they are
  evidenced by construction and true regardless of any model.
- :func:`model_narrative` asks a model for prose plus citations, and parses them
  into claims. Anything it cites that does not resolve goes to ``unverified``;
  anything it fails to cite at all goes there too.

The deterministic path is not a degraded fallback bolted on for tests. It is the
floor: with no model configured, the review still produces a real, checkable
document — just a less fluent one. A narrative that only worked when a model was
available would be a narrative whose correctness nobody could check.

:func:`compute_coverage` is the other half. It counts hunks that *no claim cites*,
and reports them by path, so the document can say out loud what it did not look
at. A tool that cannot say that cannot be trusted when it implies otherwise.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace

from forgepilot.runtime.log_guard import guarded_complete
from forgepilot.runtime.projection import build_side_request
from forgepilot.runtime.review.diffmodel import ParsedDiff
from forgepilot.seams.review import (
    ChangeGroup,
    ChangeNarrative,
    ChangeRole,
    Claim,
    Coverage,
    EvidenceKind,
    EvidenceRef,
)
from forgepilot.seams.session import EventKind

__all__ = [
    "compute_coverage",
    "deterministic_narrative",
    "model_narrative",
    "build_narrative",
    "NARRATIVE_JSON_INSTRUCTIONS",
]


NARRATIVE_JSON_INSTRUCTIONS = """\
You are writing a change narrative for a code reviewer.

Reply with JSON only, no prose around it:

{"intent":  {"text": "...", "cites": [{"kind": "diff_hunk", "hunk_id": "path#12"}]},
 "approach":[{"text": "...", "cites": [...]}],
 "risks":   [{"text": "...", "cites": [...]}]}

Rules:
- Every claim MUST carry at least one citation. A claim with no citation is
  discarded and listed as unverified, which helps nobody.
- `kind` is one of: diff_hunk (with hunk_id), session_event (with session_seq),
  code_ref (with path).
- Cite only ids you were given. Inventing one is worse than omitting a claim:
  it moves to the unverified list with the reason recorded.
- `intent` is WHY this change exists, in one or two sentences.
- `approach` is HOW, as short numbered items.
- `risks` is what a reviewer should look at hardest, by name. If you cannot point
  at something specific, return an empty list — an empty list is honest, a
  generic worry is not.
"""


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------
def compute_coverage(
    diff: ParsedDiff, groups: Sequence[ChangeGroup], *, tests_changed: bool = False
) -> Coverage:
    """Count hunks into: noise, cited, and **nobody-looked-at**.

    A hunk is "annotated" when some claim cites it. That definition is deliberate:
    it ties the coverage number to the evidence, so a narrative cannot claim to
    have covered something it never referenced. The gap between what a summary
    *sounds* like it covered and what it cited is where reviewers get misled.
    """
    cited: set[str] = set()
    for group in groups:
        for ref in group.rationale.evidence:
            if ref.kind is EvidenceKind.DIFF_HUNK and ref.hunk_id:
                cited.add(ref.hunk_id)

    hunks = diff.hunks
    noise = [h for h in hunks if h.is_noise]
    substantive = [h for h in hunks if not h.is_noise]
    annotated = [h for h in substantive if h.hunk_id in cited]
    unexamined = [h for h in substantive if h.hunk_id not in cited]

    any_tests = tests_changed or any(g.role is ChangeRole.TESTS and g.hunks for g in groups)

    return Coverage(
        total_hunks=len(hunks),
        annotated_hunks=len(annotated),
        noise_hunks=len(noise),
        unexamined_hunks=len(unexamined),
        total_files=len(diff.files),
        total_added=diff.added,
        total_removed=diff.removed,
        unexamined=tuple(sorted({h.path for h in unexamined})),
        tests_changed=any_tests,
    )


def _summary_line(diff: ParsedDiff, coverage: Coverage) -> str:
    return (
        f"{coverage.total_files} 个文件 · +{diff.added} −{diff.removed} · "
        f"{coverage.total_hunks} 个块"
    )


# ---------------------------------------------------------------------------
# Deterministic narrative
# ---------------------------------------------------------------------------
def deterministic_narrative(
    *,
    title: str,
    diff: ParsedDiff,
    groups: Sequence[ChangeGroup],
    blast_radius=None,
    session_id: str = "",
    ticket_id: str = "",
) -> ChangeNarrative:
    """A narrative built only from the diff. Every claim is evidenced by construction."""
    from forgepilot.seams.review import BlastRadius

    hunk_refs = tuple(
        EvidenceRef(
            kind=EvidenceKind.DIFF_HUNK,
            hunk_id=h.hunk_id,
            detail=f"{h.path} +{len(h.added)} −{len(h.removed)}",
        )
        for h in diff.hunks
        if not h.is_noise
    )

    touched = ", ".join(f"`{p}`" for p in diff.paths[:6])
    more = f" 等 {len(diff.paths)} 个文件" if len(diff.paths) > 6 else ""
    intent_text = f"本次改动涉及 {touched}{more}，共 +{diff.added} −{diff.removed}。"
    if ticket_id:
        intent_text = f"对应工单 {ticket_id}。" + intent_text

    intent = Claim(text=intent_text, evidence=hunk_refs[:20])

    approach = tuple(
        Claim(text=f"{group.title}：{group.rationale.text}", evidence=group.rationale.evidence)
        for group in groups
        if group.role is not ChangeRole.NOISE
    )

    risks: list[Claim] = []
    if not coverage_tests(diff):
        risks.append(
            Claim(
                text="这次改动没有包含任何测试文件的修改，改动是否被断言过无法从 diff 看出。",
                evidence=hunk_refs[:5],
            )
        )

    coverage = compute_coverage(diff, groups)
    return ChangeNarrative(
        title=title,
        summary_line=_summary_line(diff, coverage),
        intent=intent,
        approach=approach,
        groups=tuple(groups),
        blast_radius=blast_radius or BlastRadius(),
        risks=tuple(risks),
        coverage=coverage,
        session_id=session_id,
        meta={"generator": "deterministic"},
    )


def coverage_tests(diff: ParsedDiff) -> bool:
    from forgepilot.runtime.review.grouping import assign_role

    return any(
        assign_role(f.path, noise=f.noise is not None) is ChangeRole.TESTS for f in diff.files
    )


# ---------------------------------------------------------------------------
# Model narrative
# ---------------------------------------------------------------------------
def _parse_cites(raw: object) -> tuple[EvidenceRef, ...]:
    if not isinstance(raw, list):
        return ()
    refs: list[EvidenceRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", ""))
        try:
            if kind == "diff_hunk" and item.get("hunk_id"):
                refs.append(EvidenceRef(kind=EvidenceKind.DIFF_HUNK, hunk_id=str(item["hunk_id"])))
            elif kind == "session_event" and item.get("session_seq") is not None:
                refs.append(
                    EvidenceRef(
                        kind=EvidenceKind.SESSION_EVENT, session_seq=int(item["session_seq"])
                    )
                )
            elif kind == "code_ref" and item.get("path"):
                from forgepilot.seams.refs import CodeRef, RepoRef

                refs.append(
                    EvidenceRef(
                        kind=EvidenceKind.CODE_REF,
                        code_ref=CodeRef(
                            repo=RepoRef(host="", namespace="", name=""),
                            path=str(item["path"]),
                            ref=str(item.get("ref", "HEAD")),
                        ),
                    )
                )
        except (TypeError, ValueError):
            # A malformed citation is dropped, so the claim it belongs to ends up
            # unevidenced and lands in `unverified` — which is the honest outcome.
            continue
    return tuple(refs)


def parse_model_narrative(payload: str) -> dict[str, tuple[Claim, ...] | Claim] | None:
    """Parse the model's JSON into claims. Returns ``None`` if it is unusable."""
    text = payload.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, IndexError):
        return None
    if not isinstance(data, dict):
        return None

    def claim_of(node: object) -> Claim | None:
        if isinstance(node, str):
            return Claim(text=node)
        if isinstance(node, dict) and node.get("text"):
            return Claim(text=str(node["text"]), evidence=_parse_cites(node.get("cites")))
        return None

    out: dict[str, tuple[Claim, ...] | Claim] = {}
    intent = claim_of(data.get("intent"))
    if intent is not None:
        out["intent"] = intent
    for key in ("approach", "risks"):
        items = data.get(key) or []
        parsed = tuple(c for c in (claim_of(i) for i in items) if c is not None)
        out[key] = parsed
    return out


async def model_narrative(
    *,
    base: ChangeNarrative,
    session: object,
    llm: object,
    model: str = "",
    user_brief: str = "",
) -> ChangeNarrative | None:
    """Ask a model to enrich the narrative. ``None`` if it cannot be used.

    Returning ``None`` rather than raising keeps the caller simple: a model that
    is unavailable, slow or incoherent degrades to the deterministic narrative
    instead of failing the review.

    The prompt is built by :func:`forgepilot.runtime.projection.build_side_request`
    and the call goes through
    :func:`forgepilot.runtime.log_guard.guarded_complete`. This request is not part
    of the conversation — the diff is not history the log holds — so it cannot be
    ``derive_messages`` of anything; the ``review/request`` event is what makes the
    call reconstructable afterwards, and it records the prompt verbatim.
    """
    available = (
        "\n".join(
            f"- hunk_id={h.hunk_id}  {h.path} +{len(h.added)} −{len(h.removed)}"
            for h in base.groups[0].hunks[:40]
        )
        if base.groups
        else ""
    )
    event_lines = "\n".join(
        f"- session_seq={e.seq}  {e.kind}"
        for e in list(getattr(session, "committed_events", ()) or ())[:60]
    )

    request = await build_side_request(
        session,
        kind=EventKind.REVIEW_REQUEST,
        model=model or getattr(llm, "model", ""),
        system=(NARRATIVE_JSON_INSTRUCTIONS,),
        prompt=(
            f"{user_brief}\n\n可引用的 hunk：\n{available}\n\n可引用的会话事件：\n{event_lines}"
        ),
        max_tokens=2000,
    )

    try:
        response = await guarded_complete(session, llm, request)
    except Exception:  # noqa: BLE001 - any model failure degrades, never fails the review
        return None

    parsed = parse_model_narrative(response.message.content or "")
    if not parsed:
        return None

    intent = parsed.get("intent")
    approach = parsed.get("approach") or ()
    risks = parsed.get("risks") or ()

    return replace(
        base,
        intent=intent if isinstance(intent, Claim) else base.intent,
        approach=tuple(approach) or base.approach,
        risks=tuple(risks) if risks else base.risks,
        meta={**dict(base.meta), "generator": "model"},
    )


async def build_narrative(
    *,
    title: str,
    diff: ParsedDiff,
    groups: Sequence[ChangeGroup],
    blast_radius=None,
    session: object = None,
    llm: object = None,
    model: str = "",
    user_brief: str = "",
    ticket_id: str = "",
    use_model: bool = True,
) -> ChangeNarrative:
    """Deterministic narrative first, optionally enriched by a model, then validated."""
    from forgepilot.runtime.review.validate import EvidenceUniverse, validate_narrative

    session_id = ""
    if session is not None:
        header = getattr(session, "header", None)
        session_id = str(getattr(header, "session_id", ""))

    narrative = deterministic_narrative(
        title=title,
        diff=diff,
        groups=groups,
        blast_radius=blast_radius,
        session_id=session_id,
        ticket_id=ticket_id,
    )

    if use_model and llm is not None and session is not None:
        enriched = await model_narrative(
            base=narrative, session=session, llm=llm, model=model, user_brief=user_brief
        )
        if enriched is not None:
            narrative = enriched

    universe = EvidenceUniverse(
        high_water=max((e.seq for e in getattr(session, "committed_events", ()) or ()), default=0),
        changed_paths=diff.paths,
        hunk_ids=[h.hunk_id for h in diff.hunks],
        check_events=[
            e.seq for e in getattr(session, "committed_events", ()) or () if e.kind == "task/check"
        ],
    )
    narrative = validate_narrative(narrative, universe)
    return replace(narrative, coverage=compute_coverage(diff, narrative.groups))
