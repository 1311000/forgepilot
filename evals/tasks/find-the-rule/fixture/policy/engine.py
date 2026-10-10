"""Apply every rule and collect what the user has to read.

The engine is deliberately stupid: it does not stop at the first failure, does not
rank them and does not know what any of them mean. Everything a user sees comes
from the rule that produced it, which is the only way the message can be trusted to
match the check.
"""

from __future__ import annotations

from policy.rules import RULES


def check(candidate: str) -> list[str]:
    """The messages for every rule ``candidate`` breaks, in reading order."""
    return [rule.message for rule in RULES if rule.violated(candidate)]


def broken_rules(candidate: str) -> list[str]:
    """The same failures by name. Used by the audit job, never shown to a user."""
    return [rule.name for rule in RULES if rule.violated(candidate)]
