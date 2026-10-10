"""Every rule the engine applies, in the order a user should read the failures."""

from __future__ import annotations

from policy.rules import (
    dictionary,
    digits,
    keyboard,
    length,
    normalisation,
    runs,
    sequences,
    variety,
)

RULES = (
    length.RULE,
    digits.RULE,
    variety.RULE,
    runs.RULE,
    sequences.RULE,
    keyboard.RULE,
    dictionary.RULE,
    normalisation.RULE,
)
