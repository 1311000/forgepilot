"""Distillation seam — service name ``distillationWatermarks``.

**What is swappable here is the watermark, not the distillation.** Turning a log
into memories is a procedure with a fixed shape (`runtime/distillation.py`), and
the thing a deployment actually needs to configure is *how far each session has
been distilled* — a durable fact that must survive the process that produced it.
So that is the seam; there is deliberately no `Distiller` protocol, because two
implementations of the trust rule would be two different answers to "what may be
believed", which is the one question this milestone exists to answer once.

**The division of labour is the whole point, and it is not a convention here.**
The model extracts: it reads a session and proposes what was worth keeping. The
harness decides: it resolves each proposal's *evidence* against the log and
computes the trust kind itself. A model can therefore not raise its own
confidence — it cannot even name a `confidence`, a `status`, a `tenant_id` or an
`id`, because none of those fields exist in the schema it is asked to fill in
(see `parse_candidates`, which rejects extra fields rather than ignoring them).

The three rules below are the ones a reader should check the code against:

- **An invented sequence number is not evidence.** A candidate cites
  `evidence_seqs`; those seqs must exist in *this* log, and the trust kind comes
  from the events at those seqs, not from the candidate's `type` or from the
  model's hint.
- **Reading is not believing.** A cited `read_file` or `code_search` result is
  recorded as provenance — a memory stays traceable to what was read — but it
  confers **no** trust: "we read that the vendor directory is generated" is not
  "the vendor directory is generated". Otherwise reading a hostile file would
  launder its contents into memory, which is the same defect as a check that
  cannot fail, one indirection further away.
- **A check that could not run is not a passing check.** `task/check` carries
  `couldNotRun` alongside `passed`; citing a check that never ran must not confer
  `TEST_VERIFIED`, or a broken test runner would manufacture verified memories.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from forgepilot.errors import ForgePilotError
from forgepilot.seams.memory import EventRef, MemoryType, ProvenanceKind
from forgepilot.seams.refs import RepoRef
from forgepilot.seams.session import EventKind, SessionEvent

__all__ = [
    "SERVICE_NAME",
    "DistillationError",
    "DistillationSchemaError",
    "DistillCandidate",
    "DistillationReport",
    "Witness",
    "WatermarkStore",
    "parse_candidates",
    "witness_for",
    "EFFECT_TOOL_NAMES",
    "READ_ONLY_TOOL_NAMES",
    "CANDIDATE_FIELDS",
    "MAX_CANDIDATES_PER_RUN",
    "MAX_EVIDENCE_SEQS",
]

SERVICE_NAME = "distillationWatermarks"


class DistillationError(ForgePilotError):
    """Distillation could not produce memories, and the reason is not the model's."""


class DistillationSchemaError(DistillationError):
    """The model's answer was not the JSON this asked for.

    Raised for a malformed document, a missing field, a field the schema does not
    define, a wrong type, or too many candidates. **Extra fields are refused
    rather than dropped**: a model that volunteers `confidence` or `status` is
    answering a question it was not asked, and silently discarding the field
    would leave the next reader believing the model's numbers were considered.
    """


#: An extraction that returns more than this is not a distillation, it is a
#: rewrite of the log. Refused rather than truncated: truncating would keep the
#: first 64 of 500 proposals and look like a complete run.
MAX_CANDIDATES_PER_RUN = 64

#: A candidate citing more events than this is refused. The cap is not about
#: security — it is that `Provenance.evidence_refs` is copied into the record and
#: into `memory/write`, and "everything" is not evidence for anything.
MAX_EVIDENCE_SEQS = 32

#: The fields the model may fill in. Everything else about a memory — status,
#: confidence, provenance, tenant, id, timestamps — is the harness's to set.
CANDIDATE_FIELDS = frozenset({"type", "subject", "text", "evidence_seqs", "model_confidence_hint"})

#: Tools whose **result** witnesses an effect: something ran, or something was
#: written. A result from one of these, when it did not fail, is what
#: `TOOL_OBSERVED` means.
EFFECT_TOOL_NAMES = frozenset({"run", "write_file", "edit_file"})

#: Tools that only look. Listed explicitly — rather than "anything not in the
#: effect set" — so a newly shipped tool is *unclassified* and confers no trust
#: until someone decides, instead of inheriting a trust level nobody chose.
READ_ONLY_TOOL_NAMES = frozenset({"read_file", "code_search"})

_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(?P<body>.*?)\n?\s*```\s*$", re.DOTALL)


@dataclass(frozen=True, slots=True)
class DistillCandidate:
    """One thing the model thinks is worth remembering, and where it saw it."""

    type: MemoryType
    subject: str
    text: str
    evidence_seqs: tuple[int, ...]
    model_confidence_hint: float | None = None

    def to_event_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": str(self.type),
            "subject": self.subject,
            "text": self.text,
            "evidenceSeqs": list(self.evidence_seqs),
        }
        if self.model_confidence_hint is not None:
            data["modelConfidenceHint"] = self.model_confidence_hint
        return data


def parse_candidates(
    raw: str, *, limit: int = MAX_CANDIDATES_PER_RUN
) -> tuple[DistillCandidate, ...]:
    """Turn the model's answer into candidates, or refuse it.

    Packaging is tolerated: a fenced ```` ```json ```` block, or prose around a
    fenced block, is what models actually emit, and rejecting it would be
    rejecting formatting rather than content. **Fields are not tolerated.** An
    unknown key, a missing key, a `type` outside :class:`MemoryType`, a boolean
    where a sequence number belongs — each is an error naming the offender, since
    the alternative is a memory whose trust was derived from a field the model
    invented.
    """
    body = _unfence(raw)
    if not body:
        raise DistillationSchemaError("the model returned nothing to parse")
    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DistillationSchemaError(
            f"the model's answer is not JSON: {exc}. It must be a JSON array of "
            f"objects with fields {sorted(CANDIDATE_FIELDS)}."
        ) from exc

    if not isinstance(document, list):
        raise DistillationSchemaError(
            f"the model's answer is a {type(document).__name__}, not a JSON array. "
            f"Even one candidate must be wrapped in a list."
        )
    if len(document) > limit:
        raise DistillationSchemaError(
            f"the model proposed {len(document)} candidates, which is more than the "
            f"{limit} a distillation may return. A run that rewrites the log is not an "
            f"extraction, and keeping the first {limit} would hide that it happened."
        )

    candidates: list[DistillCandidate] = []
    for index, entry in enumerate(document):
        candidates.append(_candidate(entry, index=index))
    return tuple(candidates)


def _unfence(raw: str) -> str:
    """The text inside a fenced block, or the text as given."""
    text = (raw or "").strip()
    fenced = _FENCE.match(text)
    if fenced is not None:
        return fenced.group("body").strip()
    # A model that wraps its JSON in prose plus a fence: take the outer fence.
    start = text.find("```")
    if start != -1:
        end = text.rfind("```")
        if end > start:
            inner = text[start + 3 : end]
            if inner.lstrip().lower().startswith("json"):
                inner = inner.lstrip()[4:]
            return inner.strip()
    return text


def _candidate(entry: Any, *, index: int) -> DistillCandidate:
    where = f"candidate #{index}"
    if not isinstance(entry, Mapping):
        raise DistillationSchemaError(f"{where} is a {type(entry).__name__}, not an object")
    keys = set(entry)
    unknown = sorted(keys - CANDIDATE_FIELDS)
    if unknown:
        raise DistillationSchemaError(
            f"{where} carries field(s) {unknown}, which the schema does not define. "
            f"Status, confidence, provenance, tenant and id are assigned by the "
            f"harness from the cited events — a model that supplies them is answering "
            f"a question it was not asked."
        )
    missing = sorted({"type", "subject", "text", "evidence_seqs"} - keys)
    if missing:
        raise DistillationSchemaError(f"{where} is missing field(s) {missing}")

    try:
        kind = MemoryType(str(entry["type"]))
    except ValueError as exc:
        raise DistillationSchemaError(
            f"{where} has type {entry['type']!r}, which is not one of "
            f"{[str(m) for m in MemoryType]}"
        ) from exc

    subject = str(entry["subject"]).strip()
    text = str(entry["text"]).strip()
    if not subject or not text:
        raise DistillationSchemaError(
            f"{where} has an empty {'subject' if not subject else 'text'}; a memory "
            f"with no subject cannot take part in conflict detection, and one with no "
            f"text says nothing."
        )

    seqs_raw = entry["evidence_seqs"]
    if not isinstance(seqs_raw, Sequence) or isinstance(seqs_raw, (str, bytes)):
        raise DistillationSchemaError(
            f"{where}'s evidence_seqs is a {type(seqs_raw).__name__}, not a list"
        )
    if len(seqs_raw) > MAX_EVIDENCE_SEQS:
        raise DistillationSchemaError(
            f"{where} cites {len(seqs_raw)} events, more than the {MAX_EVIDENCE_SEQS} "
            f"allowed. A citation list that long is not evidence for one claim."
        )
    seqs: list[int] = []
    for value in seqs_raw:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DistillationSchemaError(
                f"{where}'s evidence_seqs holds {value!r}, which is not a sequence number"
            )
        seqs.append(value)

    hint = entry.get("model_confidence_hint")
    if hint is not None:
        if isinstance(hint, bool) or not isinstance(hint, (int, float)):
            raise DistillationSchemaError(
                f"{where}'s model_confidence_hint is {hint!r}, not a number"
            )
        hint = float(hint)
        if not 0.0 <= hint <= 1.0:
            raise DistillationSchemaError(
                f"{where}'s model_confidence_hint is {hint}, outside 0..1"
            )

    return DistillCandidate(
        type=kind,
        subject=subject,
        text=text,
        evidence_seqs=tuple(seqs),
        model_confidence_hint=hint,
    )


@dataclass(frozen=True, slots=True)
class Witness:
    """What a candidate's cited events actually support, decided by the harness."""

    kind: ProvenanceKind
    evidence: tuple[EventRef, ...]
    #: Why this kind — written into `memory/write` so an auditor can see the
    #: harness's reasoning rather than re-deriving it from the seq numbers.
    reason: str

    @property
    def is_recallable(self) -> bool:
        return self.kind in (
            ProvenanceKind.HUMAN_VERIFIED,
            ProvenanceKind.TEST_VERIFIED,
            ProvenanceKind.TOOL_OBSERVED,
        )


def witness_for(
    log: Sequence[SessionEvent],
    cited: Sequence[int],
    *,
    session_id: str,
    repo: RepoRef | None = None,
) -> Witness:
    """Resolve the trust a candidate's citations are worth.

    ``log`` is the whole session — the tool *names* live on `tool/call` events
    that a candidate has no reason to cite, so pairing results to their calls
    needs the full log, not just the cited slice.

    ``cited`` must already have been checked for existence; this function does not
    raise for an unknown seq, it simply has nothing to say about it (the caller
    drops such a candidate *before* asking, which is the check that makes an
    invented seq harmless).
    """
    by_seq = {event.seq: event for event in log}
    tool_names = {
        str(event.data.get("callId")): str(event.data.get("name"))
        for event in log
        if event.kind == EventKind.TOOL_CALL
    }
    events = [by_seq[seq] for seq in cited if seq in by_seq]

    human = [e for e in events if e.kind == EventKind.USER_MESSAGE]
    verified = [
        e
        for e in events
        if e.kind == EventKind.TASK_CHECK
        and bool(e.data.get("passed"))
        and not e.data.get("couldNotRun")
    ]
    observed = [
        e
        for e in events
        if e.kind == EventKind.TOOL_RESULT
        and not e.data.get("isError")
        and tool_names.get(str(e.data.get("callId"))) in EFFECT_TOOL_NAMES
    ]

    evidence = tuple(EventRef(session_id=session_id, seq=event.seq) for event in events)

    if human:
        # **What `HUMAN_VERIFIED` means, precisely.** It does not mean "this is
        # true" — nobody can check that. It means *the human said it at the cited
        # seq, and you can go read the words*. A model that cites a user message
        # for a claim the user never made produces a mis-attributed memory, not a
        # verified one; that is why the citation is stored on the record and why
        # Phase F keeps both sides of a conflict instead of picking one.
        return Witness(
            kind=ProvenanceKind.HUMAN_VERIFIED,
            evidence=evidence,
            reason=(
                f"the user's own message {human[0].seq} is cited, so the claim is "
                f"attributed to the human and readable at that event"
            ),
        )
    if verified:
        return Witness(
            kind=ProvenanceKind.TEST_VERIFIED,
            evidence=evidence,
            reason=(
                f"the check at {verified[0].seq} passed "
                f"({verified[0].data.get('command', 'a command')}), and it really ran"
            ),
        )
    if observed:
        return Witness(
            kind=ProvenanceKind.TOOL_OBSERVED,
            evidence=evidence,
            reason=(
                f"the result at {observed[0].seq} is from "
                f"{tool_names.get(str(observed[0].data.get('callId')))} and did not fail"
            ),
        )

    # Nothing cited endorses the claim. The reason still names the near misses: a
    # cited check that never ran, a cited error, the agent's own words. "Never
    # trusted" and "trusted and then found wanting" are different facts about a
    # model's answer, and only the second is a prompt worth fixing.
    near: list[str] = []
    reads = [
        e
        for e in events
        if e.kind == EventKind.TOOL_RESULT
        and tool_names.get(str(e.data.get("callId"))) in READ_ONLY_TOOL_NAMES
    ]
    if reads:
        near.append(
            "the only results cited are reads, and reading that something is so is "
            "not evidence that it is so"
        )
    for check in events:
        if check.kind != EventKind.TASK_CHECK:
            continue
        if check.data.get("couldNotRun"):
            near.append(f"the check at {check.seq} could not run, so it reports nothing")
        elif not check.data.get("passed"):
            near.append(f"the check at {check.seq} did not pass")
    for failure in events:
        if failure.kind == EventKind.TOOL_RESULT and failure.data.get("isError"):
            near.append(f"the result at {failure.seq} is an error, not an observation")
    for said in events:
        if said.kind == EventKind.ASSISTANT_MESSAGE:
            near.append(f"the assistant's message {said.seq} is what the agent said, not evidence")
    return Witness(
        kind=ProvenanceKind.MODEL_ASSERTED,
        evidence=evidence,
        reason="; ".join(near) or "nothing cited endorses the claim",
    )


@dataclass(frozen=True, slots=True)
class DistillationReport:
    """What one distillation run did — printed by the CLI, logged as events."""

    session_id: str
    watermark_before: int = 0
    watermark_after: int = 0
    events: int = 0
    turns: int = 0
    candidates: int = 0
    written: int = 0
    provisional: int = 0
    duplicated: int = 0
    dropped: int = 0
    #: Candidates the **store** refused, and therefore the reason the watermark did
    #: not move past them. Separate from ``dropped`` because only one of the two is a
    #: property of the text: a citation of an event the window never showed is
    #: settled (the same window would produce it again), while a store refusal is a
    #: property of the store *and its configuration at that moment* — a migration, a
    #: scope rule or a tenant change can make the same claim admissible tomorrow.
    deferred: int = 0
    #: Empty when the run was fine. Otherwise a short code — `llm`, `schema`,
    #: `budget`, `no-scope`, `no-tenant`, `no-model` — chosen so a caller can
    #: branch without parsing prose. The last three are pre-flight refusals: the
    #: run never started, so it emits no events and prints its reason instead.
    failed: str = ""
    detail: str = ""
    memories: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    dry_run: bool = False
    finished_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_event_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "session": self.session_id,
            "watermark": [self.watermark_before, self.watermark_after],
            "events": self.events,
            "turns": self.turns,
            "candidates": self.candidates,
            "written": self.written,
            "provisional": self.provisional,
            "duplicated": self.duplicated,
            "dropped": self.dropped,
            "deferred": self.deferred,
            "memories": list(self.memories),
            "tokens": {
                "input": self.input_tokens,
                "output": self.output_tokens,
            },
            "costUsd": self.cost_usd,
            "dryRun": self.dry_run,
        }
        if self.failed:
            data["failed"] = self.failed
            data["detail"] = self.detail
        if self.notes:
            data["notes"] = list(self.notes)
        return data


@runtime_checkable
class WatermarkStore(Protocol):
    """How far each session's log has been distilled.

    Needed for two separate reasons, and both are load-bearing:

    - **Cost.** Distilling a session every time a timer fires pays for the same
      extraction over and over.
    - **Correctness.** Without it, a second run over the same stretch writes the
      same claim a second time — de-duplication by content hash would collapse
      them, but only if the extraction is identical, and a model is not.

    It must be **durable**: the process that distils a session is usually not the
    process that ran it, and is definitely not the next one.
    """

    async def get(self, session_id: str) -> int:
        """The last seq already distilled, or 0 for "nothing yet"."""
        ...

    async def set(self, session_id: str, seq: int) -> None:
        """Record that everything up to ``seq`` has been distilled."""
        ...
