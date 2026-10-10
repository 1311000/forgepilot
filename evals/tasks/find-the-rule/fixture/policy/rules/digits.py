"""A digit somewhere in the credential.

Not because a digit is worth much on its own — it is worth about one bit — but
because the policy is published and a published policy that omits it invites the
question of what else it omits.
"""

from __future__ import annotations

from policy.rules.base import Rule


def violated(candidate: str) -> bool:
    return not any(character.isascii() and character.isdigit() for character in candidate)


RULE = Rule(
    name="digits",
    message="must contain at least one digit",
    violated=violated,
)
