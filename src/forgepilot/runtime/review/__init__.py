"""Building a change narrative.

The pieces, in the order they run::

    diffmodel   -> parse hunks; classify noise; pair renames
    grouping    -> assign roles and put them in reading order
    blastradius -> what else the change touches (AST when available)
    narrative   -> the model's prose, with evidence attached
    validate    -> every claim must resolve, or it moves to `unverified`
    render_*    -> Markdown for a human, JSON for the UI

The ordering step is the one worth reading first: it is where this stops being a
summariser. See `grouping.py`.
"""

from forgepilot.runtime.review.diffmodel import (
    ParsedFile,
    ParsedDiff,
    parse_diff,
    classify_noise,
    detect_renames,
)
from forgepilot.runtime.review.grouping import group_hunks, order_groups
from forgepilot.runtime.review.blastradius import (
    analysis_backend,
    ast_available,
    analyse_blast_radius,
)
from forgepilot.runtime.review.narrative import build_narrative, compute_coverage
from forgepilot.runtime.review.validate import EvidenceUniverse, validate_narrative
from forgepilot.runtime.review.render_markdown import render_markdown
from forgepilot.runtime.review.render_json import render_json

__all__ = [
    "ParsedFile",
    "ParsedDiff",
    "parse_diff",
    "classify_noise",
    "detect_renames",
    "group_hunks",
    "order_groups",
    "analysis_backend",
    "ast_available",
    "analyse_blast_radius",
    "build_narrative",
    "compute_coverage",
    "EvidenceUniverse",
    "validate_narrative",
    "render_markdown",
    "render_json",
]
