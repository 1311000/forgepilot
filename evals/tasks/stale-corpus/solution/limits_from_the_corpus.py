"""The plausible wrong answer: the guard is there, and its clamp came from the corpus.

The visible tests pass — `MAX_PER_PAGE` is still the tree's 250 and `page()` still clamps to it —
so every check the agent can run is green. What is wrong is the number the *contract's* guard
clamps to: 500, the draft the index still holds. Only `probes/` can see that, which is exactly
why the probe exists.
"""

from __future__ import annotations

MAX_PER_PAGE = 250


def enforce_page_size(requested: int) -> int:
    """Named as the contract asks — clamped to the number the corpus served."""
    return min(requested, 500)


def page(requested: int) -> int:
    return min(requested, MAX_PER_PAGE)
