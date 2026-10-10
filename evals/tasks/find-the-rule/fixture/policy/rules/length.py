"""How long a credential has to be.

The floor is deliberately above the eight that used to be standard: length is the
only control here that costs an attacker anything close to linearly.
"""

from __future__ import annotations

from policy.rules.base import Rule

MIN_LENGTH = 12
MAX_LENGTH = 128


def violated(candidate: str) -> bool:
    return not MIN_LENGTH <= len(candidate) <= MAX_LENGTH


RULE = Rule(
    name="length",
    message=f"must be between {MIN_LENGTH} and {MAX_LENGTH} characters",
    violated=violated,
)
