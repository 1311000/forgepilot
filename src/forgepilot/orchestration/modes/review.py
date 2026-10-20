"""Review mode: turn a diff into a change narrative a person can actually use.

Thin by design — the work is in ``runtime/review/``, and this composes it::

    parse the diff -> fold noise -> pair renames -> group and order
        -> analyse impact -> assemble -> validate -> render

The one thing worth noticing is what this mode is *not* doing. It is not
summarising. Ordering and coverage accounting are the product; the prose is the
part a model is allowed to help with, and it is the only part that can be
discarded without losing the document's value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forgepilot.runtime.review.blastradius import analyse_blast_radius, analysis_backend
from forgepilot.runtime.review.diffmodel import classify_noise, detect_renames, parse_diff
from forgepilot.runtime.review.grouping import group_hunks
from forgepilot.runtime.review.narrative import build_narrative
from forgepilot.seams.refs import RepoRef
from forgepilot.seams.review import AnalysisBackend, ChangeNarrative

__all__ = ["ReviewPlan", "prepare_review", "build_review", "read_workspace_sources"]


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    """The parsed, ordered, noise-folded view of a diff — before any prose."""

    parsed: Any
    groups: tuple[Any, ...]
    blast_radius: Any
    backend: AnalysisBackend

    @property
    def file_count(self) -> int:
        return len(self.parsed.files)

    @property
    def hunk_count(self) -> int:
        return len(self.parsed.hunks)


def read_workspace_sources(root: Path, paths: Sequence[str]) -> dict[str, str]:
    """Read the current content of some files, for symbol and reference analysis.

    Missing files are skipped rather than raising: a deleted file legitimately has
    no current content, and the analysis should cope with that rather than fail.
    """
    out: dict[str, str] = {}
    for relative in paths:
        candidate = root / relative
        try:
            if candidate.is_file():
                out[relative] = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return out


def prepare_review(
    diff_text: str,
    *,
    workspace_root: Path | None = None,
    workspace_sources: Mapping[str, str] | None = None,
    repo: RepoRef | None = None,
    ref: str = "HEAD",
) -> ReviewPlan:
    """Everything deterministic: parse, fold noise, pair renames, order, analyse.

    No model is involved, which is what makes the ordering and the impact analysis
    checkable. The prose is added afterwards and can be thrown away.
    """
    parsed = detect_renames(classify_noise(parse_diff(diff_text)))

    sources = dict(workspace_sources or {})
    if workspace_root is not None and not sources:
        sources = read_workspace_sources(workspace_root, parsed.paths)

    groups = group_hunks(parsed, sources=sources)

    removed = [
        line.text for hunk in parsed.hunks for line in hunk.lines if line.kind.value == "remove"
    ]
    changed_sources = {p: sources[p] for p in parsed.paths if p in sources}
    blast_radius = analyse_blast_radius(
        changed_sources=changed_sources,
        removed_lines=removed,
        workspace_sources=sources,
        repo=repo,
        ref=ref,
    )

    return ReviewPlan(
        parsed=parsed,
        groups=groups,
        blast_radius=blast_radius,
        backend=analysis_backend(),
    )


async def build_review(
    diff_text: str,
    *,
    title: str = "",
    session: Any = None,
    llm: Any = None,
    model: str = "",
    user_brief: str = "",
    ticket_id: str = "",
    workspace_root: Path | None = None,
    workspace_sources: Mapping[str, str] | None = None,
    repo: RepoRef | None = None,
    ref: str = "HEAD",
    use_model: bool = True,
) -> ChangeNarrative:
    """Prepare, optionally enrich with prose, validate, and return the narrative."""
    plan = prepare_review(
        diff_text,
        workspace_root=workspace_root,
        workspace_sources=workspace_sources,
        repo=repo,
        ref=ref,
    )

    return await build_narrative(
        title=title,
        diff=plan.parsed,
        groups=plan.groups,
        blast_radius=plan.blast_radius,
        session=session,
        llm=llm,
        model=model,
        user_brief=user_brief,
        ticket_id=ticket_id,
        use_model=use_model,
    )
