"""Unicode form.

A credential that is not in NFKC is a credential whose length, its classes and its
distinct-character count all mean something different from what the person who
typed it sees. The rule refuses rather than normalising silently, because
normalising would change what the user has to type back.
"""

from __future__ import annotations

import unicodedata

from policy.rules.base import Rule


def violated(candidate: str) -> bool:
    return unicodedata.normalize("NFKC", candidate) != candidate


RULE = Rule(
    name="normalisation",
    message="must use a single normalised form of each character",
    violated=violated,
)
