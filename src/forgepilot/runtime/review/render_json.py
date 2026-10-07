"""Rendering the narrative as JSON, for the UI that arrives in M10.

Same narrative object as the Markdown renderer, so the two cannot tell different
stories. Everything the Markdown elides for space — every hunk, every evidence
ref, every affected caller — is present here in full, because a UI can collapse
and page what a document cannot.

The fields the M10 client needs are deliberate:

- ``coverage.unexamined`` carries paths, so a coverage heat map has somewhere to
  point rather than just a number;
- ``blast_radius.affected_callers`` are full ``CodeRef``s, so hovering a symbol
  can jump to its call sites;
- every claim carries its own evidence array, so a reader can expand any single
  statement rather than trusting the document as a whole.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from forgepilot.seams.review import ChangedHunk, ChangeNarrative, Claim, DiffLineKind

__all__ = ["render_json", "to_jsonable"]


def _hunk(hunk: ChangedHunk) -> dict[str, Any]:
    return {
        "id": hunk.hunk_id,
        "path": hunk.path,
        "section": hunk.section,
        "oldStart": hunk.old_start,
        "oldCount": hunk.old_count,
        "newStart": hunk.new_start,
        "newCount": hunk.new_count,
        "added": len(hunk.added),
        "removed": len(hunk.removed),
        "noise": str(hunk.noise) if hunk.noise else None,
        "renamedFrom": hunk.renamed_from,
        "lines": [
            {"kind": str(line.kind), "text": line.text}
            for line in hunk.lines
        ],
    }


def _claim(claim: Claim) -> dict[str, Any]:
    return {
        "text": claim.text,
        "verified": claim.verified,
        "unverifiedReason": claim.unverified_reason,
        "evidence": [
            {
                "kind": str(ref.kind),
                "detail": ref.detail,
                "sessionSeq": ref.session_seq,
                "hunkId": ref.hunk_id,
                "path": getattr(ref.code_ref, "path", None),
                "ref": getattr(ref.code_ref, "ref", None),
            }
            for ref in claim.evidence
        ],
    }


def render_json(narrative: ChangeNarrative) -> dict[str, Any]:
    """A JSON-safe dict of the whole narrative."""
    return {
        "title": narrative.title,
        "summaryLine": narrative.summary_line,
        "sessionId": narrative.session_id,
        "intent": _claim(narrative.intent),
        "approach": [_claim(c) for c in narrative.approach],
        "risks": [_claim(c) for c in narrative.risks],
        "groups": [
            {
                "title": group.title,
                "role": str(group.role),
                "order": group.order,
                "positionReason": group.position_reason,
                "rationale": _claim(group.rationale),
                "paths": list(group.paths),
                "hunks": [_hunk(h) for h in group.hunks],
            }
            for group in narrative.groups
        ],
        "blastRadius": {
            "backend": str(narrative.blast_radius.backend),
            "isAst": narrative.blast_radius.is_ast,
            "note": narrative.blast_radius.note,
            "truncated": narrative.blast_radius.truncated,
            "symbols": [
                {
                    "name": s.name,
                    "kind": str(s.kind),
                    "path": s.path,
                    "newName": s.new_name,
                }
                for s in narrative.blast_radius.symbols
            ],
            # The M10 client hover target: full locations, not just counts.
            "affectedCallers": [
                {"path": ref.path, "ref": ref.ref, "repo": getattr(ref.repo, "slug", "")}
                for ref in narrative.blast_radius.affected_callers
            ],
        },
        "coverage": {
            **asdict(narrative.coverage),
            "lookedAtRatio": narrative.coverage.looked_at_ratio,
        },
        "unverified": [_claim(c) for c in narrative.unverified],
        "evidence": [
            {
                "kind": str(ref.kind),
                "detail": ref.detail,
                "sessionSeq": ref.session_seq,
                "hunkId": ref.hunk_id,
                "path": getattr(ref.code_ref, "path", None),
            }
            for ref in narrative.evidence
        ],
        "meta": dict(narrative.meta),
    }


def to_jsonable(value: Any) -> Any:
    """Best-effort conversion, for callers that kept richer objects around."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, Enum):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value
