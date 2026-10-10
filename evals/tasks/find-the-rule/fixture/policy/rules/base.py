"""What a rule is.

A rule is a name, the sentence a user reads when it fires, and a predicate. The
engine never inspects a rule's internals, which is what lets the set below grow
one module at a time without the engine changing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    message: str
    violated: Callable[[str], bool]
