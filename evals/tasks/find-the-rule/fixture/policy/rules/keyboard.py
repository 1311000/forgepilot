"""Walks along a keyboard row, and their mirrors.

`qwerty` is the one everybody knows about; `poiuyt` is the same walk in the other
direction and is tried by every tool that has a keyboard model. The rows are
stored as they are printed so a reviewer can check them against a physical one.
"""

from __future__ import annotations

from policy.rules.base import Rule

ROWS = (
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
    "1234567890",
)
WINDOW = 4


def violated(candidate: str) -> bool:
    lowered = candidate.lower()
    for row in ROWS:
        for start in range(len(row) - WINDOW + 1):
            run = row[start : start + WINDOW]
            if run in lowered or run[::-1] in lowered:
                return True
    return False


RULE = Rule(
    name="keyboard",
    message=f"must not contain {WINDOW} adjacent keys from a keyboard row",
    violated=violated,
)
