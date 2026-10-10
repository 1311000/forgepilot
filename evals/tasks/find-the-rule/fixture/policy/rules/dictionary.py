"""Words that appear at the top of every credential-stuffing list.

This is a sample, not the list. The real one is a few hundred megabytes and lives
behind the enterprise service; this module exists so the engine has something to
call, and so the rule's *shape* — case-insensitive, whole-word, and deliberately
not stemming — is written down somewhere a reviewer can disagree with.
"""

from __future__ import annotations

from policy.rules.base import Rule

COMMON = frozenset(
    {
        "password",
        "letmein",
        "welcome",
        "monkey",
        "dragon",
        "football",
        "iloveyou",
        "sunshine",
    }
)


def violated(candidate: str) -> bool:
    return candidate.lower() in COMMON


RULE = Rule(
    name="dictionary",
    message="must not be a commonly used password",
    violated=violated,
)
