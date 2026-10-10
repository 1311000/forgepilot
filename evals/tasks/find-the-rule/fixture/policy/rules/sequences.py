"""Ascending runs: `abcd`, `4321`, `qwerty`'s cousins on the number row.

The window is four because three collides with ordinary words — `deed`, `feed`,
`adds` — and a rule that refuses `passwordabc` is a rule nobody keeps.
"""

from __future__ import annotations

from policy.rules.base import Rule

WINDOW = 4


def violated(candidate: str) -> bool:
    lowered = candidate.lower()
    for start in range(len(lowered) - WINDOW + 1):
        window = [ord(character) for character in lowered[start : start + WINDOW]]
        if len(set(window)) == WINDOW and (
            window == list(range(window[0], window[0] + WINDOW))
            or window == list(range(window[0], window[0] - WINDOW, -1))
        ):
            return True
    return False


RULE = Rule(
    name="sequences",
    message=f"must not contain {WINDOW} consecutive characters in order",
    violated=violated,
)
