"""A floor on how many distinct characters appear.

This is the cheap stand-in for entropy. It catches `aaaa...aaaa1` and the other
things people type when a counter has told them the password is too short.
"""

from __future__ import annotations

from policy.rules.base import Rule

MIN_DISTINCT = 6


def violated(candidate: str) -> bool:
    return len(set(candidate)) < MIN_DISTINCT


RULE = Rule(
    name="variety",
    message=f"must contain at least {MIN_DISTINCT} different characters",
    violated=violated,
)
