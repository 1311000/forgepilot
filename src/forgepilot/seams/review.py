"""The change-narrative contract.

This is the shape a review produces, and its most important property is that
**a claim cannot pass as verified without evidence**.

The design is aimed at a specific failure, not at summarisation. Research on code
review keeps finding that reviewers read top-down through an alphabetically
ordered file list and their attention decays — bugs in the first file are markedly
more likely to be found than in the last — and that a change with many comments
can still have uneven coverage, because comment volume *looks* like assurance.
A narrative that merely summarises makes that worse: it looks equally thorough
whether or not it is.

So two things here are load-bearing and unusual:

- :attr:`Coverage.unexamined_hunks` — the parts **nobody looked at**. Naming them
  is the direct answer to the attention-effectiveness gap. A tool that cannot say
  "I did not examine this" cannot be trusted when it implies it examined
  everything.
- :attr:`ChangeNarrative.unverified` — claims that failed validation, kept with
  the reason. Not silently dropped, and not allowed to kill the whole review. A
  long list here is a *signal to the reviewer*: this change needs your attention.

:attr:`Claim.verified` starts ``False`` and only the validator may set it, and
only for claims whose every :class:`EvidenceRef` resolves against the session log
and the diff.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from forgepilot.seams.refs import CodeRef

__all__ = [
    "EvidenceKind",
    "EvidenceRef",
    "Claim",
    "NoiseKind",
    "ChangeRole",
    "DiffLineKind",
    "DiffLine",
    "ChangedHunk",
    "ChangeGroup",
    "SymbolChangeKind",
    "SymbolChange",
    "AnalysisBackend",
    "BlastRadius",
    "Coverage",
    "ChangeNarrative",
    "ROLE_ORDER",
    "UNVERIFIED_NO_EVIDENCE",
]


class EvidenceKind(StrEnum):
    """What a claim is pointing at."""

    #: A session-log event, cited by sequence number.
    SESSION_EVENT = "session_event"
    #: A location in the repository, cited by path and ref.
    CODE_REF = "code_ref"
    #: A check the harness ran — its exit code and output.
    CHECK = "check"
    #: A specific hunk of the diff, cited by its stable hunk id.
    DIFF_HUNK = "diff_hunk"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A pointer to something that can be checked.

    Deliberately narrow. There is no free-text variant, because "the model says
    so" is not evidence and a variant for it would eventually be used.
    """

    kind: EvidenceKind
    detail: str = ""
    #: Session-log sequence number. Validated against the log's high-water mark.
    session_seq: int | None = None
    #: Repository location. Validated against the set of changed paths.
    code_ref: CodeRef | None = None
    #: Hunk id, ``path#old_start``.
    hunk_id: str | None = None

    def describe(self) -> str:
        if self.kind is EvidenceKind.SESSION_EVENT:
            return f"#{self.session_seq}"
        if self.kind is EvidenceKind.CODE_REF and self.code_ref is not None:
            return f"{self.code_ref.path}@{self.code_ref.ref}"
        if self.kind is EvidenceKind.DIFF_HUNK:
            return str(self.hunk_id)
        return self.detail or str(self.kind)


#: Reason attached to a claim the validator had to move out of the verified set.
UNVERIFIED_NO_EVIDENCE = "no evidence was cited"


@dataclass(frozen=True, slots=True)
class Claim:
    """An assertion about the change, and what backs it.

    ``verified`` is set by the validator and by nothing else. Constructing a
    claim cannot make it verified, which is what stops a model-authored narrative
    from marking its own work.
    """

    text: str
    evidence: tuple[EvidenceRef, ...] = ()
    verified: bool = False
    #: Why it failed validation, when it did.
    unverified_reason: str = ""

    @property
    def is_evidenced(self) -> bool:
        return bool(self.evidence)


class NoiseKind(StrEnum):
    """Why a hunk does not deserve attention by default."""

    LOCKFILE = "lockfile"
    GENERATED = "generated"
    MINIFIED = "minified"
    VENDORED = "vendored"
    #: Every added line equals a removed line once whitespace and comments are
    #: normalised — a reformat, not a behaviour change.
    COSMETIC = "cosmetic"
    #: A pure rename, once paired with its counterpart.
    RENAME = "rename"
    BINARY = "binary"


class ChangeRole(StrEnum):
    """What a changed file *is*, which decides where it sits in the reading order."""

    SCHEMA = "schema"
    LOGIC = "logic"
    API = "api"
    CONFIG = "config"
    TESTS = "tests"
    DOCS = "docs"
    NOISE = "noise"


#: The reading order. **Not alphabetical**, and that is the point.
#:
#: Ordered so that information arrives before it is needed: the shape of the data,
#: then the logic that uses it, then the code that calls that logic, then
#: configuration, then the tests that confirm it, then prose, then things nobody
#: needs to read. This follows the "optimal order" principles from code-review
#: research, and converges with what the commercial review tools settled on
#: independently.
ROLE_ORDER: tuple[ChangeRole, ...] = (
    ChangeRole.SCHEMA,
    ChangeRole.LOGIC,
    ChangeRole.API,
    ChangeRole.CONFIG,
    ChangeRole.TESTS,
    ChangeRole.DOCS,
    ChangeRole.NOISE,
)

ROLE_TITLES: Mapping[ChangeRole, str] = {
    ChangeRole.SCHEMA: "数据层",
    ChangeRole.LOGIC: "逻辑",
    ChangeRole.API: "接口 / 调用点",
    ChangeRole.CONFIG: "配置",
    ChangeRole.TESTS: "测试",
    ChangeRole.DOCS: "文档",
    ChangeRole.NOISE: "噪声",
}


class DiffLineKind(StrEnum):
    CONTEXT = "context"
    ADD = "add"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class DiffLine:
    kind: DiffLineKind
    text: str


@dataclass(frozen=True, slots=True)
class ChangedHunk:
    """One ``@@`` block, with enough identity to be cited."""

    path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[DiffLine, ...] = ()
    #: Trailing text on the ``@@`` line — usually the enclosing function, which
    #: is often the single most useful thing to show a reviewer.
    section: str = ""
    noise: NoiseKind | None = None
    #: For a rename pair, the other side's path.
    renamed_from: str | None = None

    @property
    def hunk_id(self) -> str:
        return f"{self.path}#{self.old_start}"

    @property
    def added(self) -> tuple[str, ...]:
        return tuple(line.text for line in self.lines if line.kind is DiffLineKind.ADD)

    @property
    def removed(self) -> tuple[str, ...]:
        return tuple(line.text for line in self.lines if line.kind is DiffLineKind.REMOVE)

    @property
    def churn(self) -> int:
        return len(self.added) + len(self.removed)

    @property
    def is_noise(self) -> bool:
        return self.noise is not None


@dataclass(frozen=True, slots=True)
class ChangeGroup:
    """A logical cohort of hunks, with why it is where it is."""

    title: str
    role: ChangeRole
    rationale: Claim
    hunks: tuple[ChangedHunk, ...] = ()
    order: int = 0
    #: Why this group sits at this position in the reading order.
    position_reason: str = ""

    @property
    def churn(self) -> int:
        return sum(hunk.churn for hunk in self.hunks)

    @property
    def paths(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for hunk in self.hunks:
            seen.setdefault(hunk.path, None)
        return tuple(seen)


class SymbolChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    RENAMED = "renamed"
    SIGNATURE_CHANGED = "signature_changed"
    BODY_CHANGED = "body_changed"


@dataclass(frozen=True, slots=True)
class SymbolChange:
    name: str
    kind: SymbolChangeKind
    path: str
    new_name: str | None = None


class AnalysisBackend(StrEnum):
    """How deeply the impact analysis actually looked.

    Reported rather than assumed. A finding derived from text matching and one
    derived from an AST are not the same evidence, and presenting them
    identically would let a reader over-trust the weaker one.
    """

    AST = "ast"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class BlastRadius:
    """What else this change touches."""

    backend: AnalysisBackend = AnalysisBackend.TEXT
    symbols: tuple[SymbolChange, ...] = ()
    affected_callers: tuple[CodeRef, ...] = ()
    #: True when the search was capped, so an empty-looking result is not read as
    #: "nothing is affected".
    truncated: bool = False
    note: str = ""

    @property
    def is_ast(self) -> bool:
        return self.backend is AnalysisBackend.AST


@dataclass(frozen=True, slots=True)
class Coverage:
    """How much of the diff the narrative actually accounts for.

    The single most important number here is :attr:`unexamined_hunks`. Everything
    else in this module is in service of being able to state it.
    """

    total_hunks: int = 0
    annotated_hunks: int = 0
    noise_hunks: int = 0
    unexamined_hunks: int = 0
    total_files: int = 0
    total_added: int = 0
    total_removed: int = 0
    #: Paths of hunks nobody accounted for, so a reviewer can go straight to them.
    unexamined: tuple[str, ...] = ()
    #: Whether a test file was part of the change at all.
    tests_changed: bool = False

    @property
    def looked_at_ratio(self) -> float:
        if not self.total_hunks:
            return 1.0
        return (self.total_hunks - self.unexamined_hunks) / self.total_hunks


@dataclass(frozen=True, slots=True)
class ChangeNarrative:
    """The whole review artifact. Rendered to Markdown and to JSON from one source."""

    title: str = ""
    summary_line: str = ""
    intent: Claim = field(default_factory=lambda: Claim(text=""))
    approach: tuple[Claim, ...] = ()
    groups: tuple[ChangeGroup, ...] = ()
    blast_radius: BlastRadius = field(default_factory=BlastRadius)
    risks: tuple[Claim, ...] = ()
    #: Claims that failed validation. Kept, with reasons, and shown prominently —
    #: a long list is a signal that this change needs human attention.
    unverified: tuple[Claim, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    #: Every evidence ref cited anywhere, so a reader can enumerate the basis of
    #: the whole document rather than trusting it section by section.
    evidence: tuple[EvidenceRef, ...] = ()
    #: Session this narrative is about, for traceability.
    session_id: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def all_claims(self) -> tuple[Claim, ...]:
        claims: list[Claim] = [self.intent, *self.approach, *self.risks]
        for group in self.groups:
            claims.append(group.rationale)
        return tuple(c for c in claims if c.text)

    @property
    def verified_claims(self) -> tuple[Claim, ...]:
        return tuple(c for c in self.all_claims if c.verified)
