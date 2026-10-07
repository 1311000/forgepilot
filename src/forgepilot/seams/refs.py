"""Small shared reference objects used across seams.

These live together in one module so that seams never import each other. A seam
importing another seam would couple two replaceable capabilities and make the
three-role rule unenforceable; importing value objects from here does not.

Nothing in ``seams/`` may import ``cordis_port`` or anything from
``providers/`` — see ``tests/contract/test_import_graph.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

__all__ = [
    "Mode",
    "PrincipalId",
    "TenantId",
    "TeamId",
    "SessionId",
    "TicketId",
    "AgentId",
    "MemoryId",
    "TurnId",
    "RepoRef",
    "CommitRef",
    "BranchRef",
    "CodeRef",
    "LineSpan",
]


class Mode(StrEnum):
    """The three user-facing modes.

    They are *modes over one shared backend*, wired as bundle patches rather than
    code branches. If you find yourself branching on ``Mode`` inside runtime
    logic, the design intent is being violated — the difference belongs in
    configuration, not in an ``if``.
    """

    CHAT = "chat"
    TASK = "task"
    REVIEW = "review"

# Identifiers are distinct types so that passing a TicketId where a SessionId is
# expected is a type error rather than a silent mix-up.
PrincipalId = NewType("PrincipalId", str)
TenantId = NewType("TenantId", str)
TeamId = NewType("TeamId", str)
SessionId = NewType("SessionId", str)
TicketId = NewType("TicketId", str)
AgentId = NewType("AgentId", str)
MemoryId = NewType("MemoryId", str)
TurnId = NewType("TurnId", int)


@dataclass(frozen=True, slots=True, order=True)
class RepoRef:
    """A repository, identified independently of any local checkout.

    Deliberately host-qualified: the same namespace/name can exist on two hosts,
    and an authorisation decision that ignored the host would be a leak.
    """

    host: str
    namespace: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.host}/{self.namespace}/{self.name}"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.slug


@dataclass(frozen=True, slots=True)
class CommitRef:
    repo: RepoRef
    sha: str


@dataclass(frozen=True, slots=True)
class BranchRef:
    repo: RepoRef
    name: str
    head: CommitRef


@dataclass(frozen=True, slots=True)
class LineSpan:
    """A 1-based inclusive line range."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 1 or self.end < self.start:
            raise ValueError(f"invalid line span {self.start}..{self.end}")


@dataclass(frozen=True, slots=True)
class CodeRef:
    """A location in a repository **at a specific ref**.

    ``ref`` is mandatory, with no default. This is the type-level expression of a
    real requirement: a hit from a remote index may come from trunk while the
    developer is on a branch, so a code location that does not say which revision
    it came from is not safe to act on. Callers are expected to compare ``ref``
    against the revision they meant to read.
    """

    repo: RepoRef
    path: str
    ref: str
