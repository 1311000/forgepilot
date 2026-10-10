"""The same character, over and over.

A run is what a finger does when it rests on a key, and it is the first thing a
guessing tool tries. Three is the threshold the policy publishes.

⚠️ The predicate below does not count a *run*. It counts how often the character
appears anywhere in the credential, so `abcabc` is refused for containing `a`
twice — which is not a run, and is not what the message says.
"""

from __future__ import annotations

from policy.rules.base import Rule

RUN_LENGTH = 3


def violated(candidate: str) -> bool:
    return any(candidate.count(character) >= RUN_LENGTH - 1 for character in set(candidate))


RULE = Rule(
    name="runs",
    message=f"must not repeat a character {RUN_LENGTH} times",
    violated=violated,
)
