"""The same character, over and over.

A run is what a finger does when it rests on a key, and it is the first thing a
guessing tool tries. Three is the threshold the policy publishes.

⚠️ **The predicate counts a run now, and it did not.** It used to count how often a
character appeared *anywhere*, so `a1b2c3a1b2c3` was refused for containing `a`
twice — which is not a run, is not what the message says, and is not what the
policy means. The message was right and the check under it was measuring something
else, which is the failure mode a rule module is most likely to have: the sentence
is written once and read forever, and nothing compares it to the predicate.
"""

from __future__ import annotations

from policy.rules.base import Rule

RUN_LENGTH = 3


def violated(candidate: str) -> bool:
    """True when any character appears ``RUN_LENGTH`` times **in a row**.

    Walking adjacent pairs rather than counting occurrences: the count is what the
    rule used to do, and the difference only shows on a credential that repeats a
    character without repeating it consecutively — which is most of them.
    """
    run = 1
    for previous, current in zip(candidate, candidate[1:]):
        run = run + 1 if current == previous else 1
        if run >= RUN_LENGTH:
            return True
    return False


RULE = Rule(
    name="runs",
    message=f"must not repeat a character {RUN_LENGTH} times",
    violated=violated,
)
