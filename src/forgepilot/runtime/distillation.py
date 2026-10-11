"""Distillation: read a finished stretch of the log and turn it into memories.

**The model extracts; the harness decides.** That sentence is the design, and
everything in this module exists to keep it true. The model is shown a stretch of
the session log plus a digest of the events it may cite, and it answers with a
strict JSON array of `{type, subject, text, evidence_seqs, model_confidence_hint}`
— no `status`, no `confidence`, no `provenance`, no `tenant_id`, no `id`, because
those fields do not exist in the schema it is asked to fill in
(`seams/distillation.py::parse_candidates` refuses them rather than ignoring
them). Every one of them is then computed *here*, from the events the candidate
cited.

Four rules the code is built around, each with a failure it prevents:

1. **An invented sequence number is evidence of nothing.** A candidate may only
   cite events this run actually showed the model: seqs above the previous
   watermark, at or below the end of the last closed turn, and model-visible.
   A candidate citing anything else is dropped, with the reason recorded — the
   counterfactual is a model that cites seq 9999 and lands `ACTIVE`/`HIGH`.
2. **Reading is not believing.** `witness_for` gives `HUMAN_VERIFIED` for a cited
   user message, `TEST_VERIFIED` for a cited check that *ran* and passed,
   `TOOL_OBSERVED` for a cited result from a tool that changes something — and
   `MODEL_ASSERTED` for everything else, including a cited `read_file` result.
   Reading a hostile file must not launder its contents into memory.
3. **The watermark is what keeps a second pass from being a second extraction.**
   Every run starts after the last distilled seq, so the same stretch is never
   paid for twice; `content_hash` de-duplication in the store is the backstop, not
   the mechanism. The watermark advances only when the run succeeded, and a drop
   *does* let it advance: retrying a stretch whose citations were bad would fail
   the same way forever, which is a stall, not safety.
4. **A run that could not work never starts.** No tenant, no scope, no model name
   — each returns a report naming the reason and emits nothing, because an
   attempt that did nothing has nothing to audit.

Everything is emitted through the session log, so a distillation is itself
auditable, replayable and reconstructable: `distillation/start` is flushed on its
own (a crash then leaves an unmatched start, which is the honest record that a run
began), `distillation/request` records what was sent, `distillation/summary` keeps
the model's raw answer — including the candidates that were dropped and why —
`distillation/failed` names a run-level failure, `distillation/end` carries the
counts, and each accepted memory emits `memory/write` with the harness's reason
for its trust kind.

⚠️ **`memory/write` carries the memory's text.** That is deliberate: the user's
requirement is that a memory be traceable, auditable and *reconstructable*, and
the log is where that is possible. Two consequences are written down rather than
discovered later: the text is already redacted (the gate is at `Session.append`,
so nothing reaches this module unredacted), and a memory later revoked or purged
still has its history in an append-only log — `purge` writes a tombstone, it
cannot rewrite the past.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from forgepilot.errors import ForgePilotError
from forgepilot.runtime.log_guard import guarded_complete
from forgepilot.runtime.projection import build_distillation_request
from forgepilot.runtime.session import Session
from forgepilot.seams.distillation import (
    DistillationReport,
    DistillationSchemaError,
    WatermarkStore,
    parse_candidates,
    witness_for,
)
from forgepilot.seams.memory import (
    MemoryCandidate,
    MemoryScope,
    MemoryStore,
    Provenance,
    confidence_for,
    content_hash_of,
)
from forgepilot.seams.refs import RepoRef
from forgepilot.seams.security import Budget
from forgepilot.seams.session import EventKind, SessionEvent, SurfaceOp

__all__ = [
    "DISTILL_INSTRUCTION",
    "EVIDENCE_DIGEST_CHARS",
    "distill",
    "window_for",
]


#: The prompt. English, like every other model-facing instruction in this tree,
#: and sectioned into *what to write* and *what will be checked*, because the
#: second half is what makes a model cite events instead of asserting things.
DISTILL_INSTRUCTION = """\
You are reading one finished stretch of an enterprise coding agent's session log,
and deciding what is worth remembering in the next session.

Answer with a JSON array and nothing else — no prose, no explanation, no fenced
block unless you prefer one. Each element has exactly these fields:

  {"type": "...", "subject": "...", "text": "...", "evidence_seqs": [12, 37],
   "model_confidence_hint": 0.8}

Allowed `type` values, and what belongs in each:
  command       a command that worked, and what it was for
  preference    something the user or the team wants done a particular way
  fact          something true about this repository or its environment
  decision      a choice that was made, and the reason for it
  plan          a plan that is still in force
  task_summary  what a task accomplished and where it ended
  pitfall       something that went wrong and how it was resolved
  convention    a rule the codebase follows

`text` must stand on its own, out of context, for a reader who never saw this
session. `subject` is a short kebab-case key ("build-command", "retry-timeout");
two memories about the same thing must use the same subject.

The rules that are checked after you answer:
  * `evidence_seqs` must name events from the transcript you were shown, and only
    those. Every seq is checked against the log; a candidate citing a seq that was
    not shown to you is discarded, whole. Cite the events that actually carry the
    claim — a user's own words, a check that ran and passed, the output of a
    command that changed something.
  * What you cite decides how the memory is trusted. A cited user message is
    trusted; a cited check that ran and passed is trusted; a cited result from a
    command, write or edit is taken as observed. Citing a `read_file` or
    `code_search` result proves only that you read it, so the memory is filed as a
    proposal — which is still useful, and is the honest state for something no run
    or person confirmed.
  * A plan, a guess, or a generalisation from what you read should still be
    proposed, with the seqs that suggest it. Say less rather than more: zero
    candidates is a good answer for a session that settled nothing.
  * Never write a secret, token, password or key into `text` — it is not needed to
    remember the lesson, and the memory will outlive the session.

Return at most 20 candidates, and prefer quality: two accurate memories are worth
more than ten plausible ones. Your `model_confidence_hint` is recorded but never
used to decide trust; the harness computes that from your citations.
"""

#: How much of the event digest is allowed to reach the model. The digest is a
#: *map* of what may be cited, not the evidence itself — the transcript is already
#: in the request — so it is capped rather than streamed in full.
EVIDENCE_DIGEST_CHARS = 8000

#: One event's line in the digest.
_DIGEST_LINE_CHARS = 240


def window_for(
    events: Sequence[SessionEvent],
    *,
    watermark: int,
    upto: int | None = None,
    since: datetime | None = None,
) -> tuple[SessionEvent, ...]:
    """The stretch of log this run may look at, and the model may cite.

    Three bounds, each for a different reason:

    * **after `watermark`** — the cost and idempotency rule. Without it a timer
      re-extracts the same session forever.
    * **at or before `upto`** (the end of the last closed turn) — a turn still in
      progress has not finished happening; distilling it would file a `task_summary`
      for work whose outcome is not yet known.
    * **at or after `since`,** when given — the CLI's `--since`, applied to the
      event timestamp. An event with no readable timestamp is **kept**: dropping
      evidence because a clock field was missing is how a memory silently loses
      the thing that justified it.
    """
    if upto is None:
        upto = _closed_upto(events)
    window: list[SessionEvent] = []
    for event in events:
        if event.seq <= watermark or event.seq > upto:
            continue
        if since is not None:
            stamp = _timestamp(event)
            if stamp is not None and stamp < since:
                continue
        window.append(event)
    return tuple(window)


async def distill(
    session: Session,
    *,
    llm: Any,
    memory: MemoryStore,
    watermarks: WatermarkStore,
    scope_ref: str,
    tenant_id: str = "",
    repo: RepoRef | None = None,
    model: str = "",
    budget: Budget | None = None,
    since: datetime | None = None,
    dry_run: bool = False,
    instruction: str = DISTILL_INSTRUCTION,
    max_tokens: int = 4096,
) -> DistillationReport:
    """Distil one stretch of ``session``'s log into memories. Never raises for a
    model's failure — it reports it.

    ``budget`` is checked *after* the call, because the cost of a model call is not
    knowable before it is made. Overrunning it therefore means **nothing is
    written and the watermark does not advance**, so the stretch stays available
    for a retry with a larger cap rather than being lost.

    ``dry_run`` really calls the model and really records the run; it just does not
    touch the store and does not advance the watermark.
    """
    session_id = str(session.header.session_id)
    tenant = tenant_id or str(getattr(session.header, "tenant", "") or "")
    resolved_model = model or str(getattr(llm, "model", "") or "")
    watermark = int(await watermarks.get(session_id))
    events = tuple(session.events)
    upto = _closed_upto(events)
    window = window_for(events, watermark=watermark, upto=upto, since=since)

    report = DistillationReport(
        session_id=session_id,
        watermark_before=watermark,
        watermark_after=watermark,
        events=len(window),
        turns=sum(1 for event in window if event.kind == EventKind.TURN_END),
        dry_run=dry_run,
        finished_at=datetime.now(UTC),
    )

    # Pre-flight. Nothing has happened yet, so nothing is written — not even a
    # `distillation/start`, whose whole meaning is "a run began".
    if not window:
        return replace(
            report,
            notes=(
                f"nothing to distil: the log stands at seq "
                f"{events[-1].seq if events else 0}, distilled to seq {watermark}, "
                f"and the last closed turn ends at seq {upto}",
            ),
        )
    if not scope_ref:
        return replace(
            report,
            failed="no-scope",
            detail=(
                "a memory's audience is a repository, so distillation needs the "
                "scope_ref it writes under; it was empty"
            ),
        )
    if not tenant:
        return replace(
            report,
            failed="no-tenant",
            detail=(
                "the session names no tenant, and a memory with no tenant cannot be "
                "kept out of another tenant's recall — the store would refuse every "
                "write, after paying for the extraction"
            ),
        )
    if not resolved_model:
        return replace(
            report,
            failed="no-model",
            detail="no model name was given and the llm service declares none",
        )

    await session.append(
        EventKind.DISTILLATION_START,
        {
            "session": session_id,
            "watermark": watermark,
            "upto": upto,
            "events": report.events,
            "turns": report.turns,
            "model": resolved_model,
            "scopeRef": scope_ref,
            "tenant": tenant,
            "dryRun": dry_run,
        },
        surface=SurfaceOp.LOG_ONLY,
    )
    # Flushed on its own, like `compaction/start`: a process killed mid-run leaves
    # an unmatched start, which is the record that a run began and did not finish.
    await session.flush()

    digest = evidence_digest(window)
    request = await build_distillation_request(
        session,
        events=window,
        model=resolved_model,
        instruction=instruction,
        extra_system=(digest,) if digest else (),
        limits=(watermark, upto),
        max_tokens=max_tokens,
    )

    # A copy that carries the token counts into the failure paths. It deliberately
    # does **not** pre-set `watermark_after`: only a run that finished advances the
    # watermark, and a report claiming otherwise would contradict the `budget`
    # message three lines below it — the field is what a retry decision is made on.
    attempt = replace(report)
    try:
        response = await guarded_complete(session, llm, request)
    except Exception as exc:  # noqa: BLE001
        # A transport failure and a bug in a provider are indistinguishable at
        # this boundary, and both must leave the log with a reason rather than a
        # traceback in the middle of a maintenance pass. The message may quote a
        # remote response; the redaction gate is what keeps a credential out of it.
        return await _fail(
            session, attempt, "llm", f"{type(exc).__name__}: {exc}"
        )

    usage = response.usage
    attempt = replace(
        attempt,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=usage.cost_usd,
    )
    if budget is not None:
        spent = usage.input_tokens + usage.output_tokens
        if budget.max_tokens and spent > budget.max_tokens:
            return await _fail(
                session,
                attempt,
                "budget",
                f"the extraction used {spent} tokens and the run was capped at "
                f"{budget.max_tokens}; nothing was written and the watermark was "
                f"not advanced, so a retry with a larger cap sees this stretch again",
            )
        if budget.max_cost_usd and usage.cost_usd > budget.max_cost_usd:
            return await _fail(
                session,
                attempt,
                "budget",
                f"the extraction cost ${usage.cost_usd:.4f} and the run was capped "
                f"at ${budget.max_cost_usd:.4f}; nothing was written and the "
                f"watermark was not advanced",
            )

    raw = response.message.content or ""
    await session.append(
        EventKind.DISTILLATION_SUMMARY,
        {"session": session_id, "text": raw},
        surface=SurfaceOp.LOG_ONLY,
    )
    await session.flush()

    try:
        candidates = parse_candidates(raw)
    except DistillationSchemaError as exc:
        return await _fail(session, attempt, "schema", str(exc))

    citable = {event.seq for event in window if event.is_model_visible}
    #: Memories filed with a provenance that can be recalled.
    written: list[str] = []
    #: Memories filed as proposals. The two counts are disjoint, so `written` means
    #: "a claim an event endorses" and never "a row appeared in the store" — the
    #: difference is the whole point of the two write paths, and a report that
    #: summed them would hide it exactly where it matters (a maintenance log).
    proposed: list[str] = []
    ids_by_hash: dict[str, str] = {}
    notes: list[str] = []
    dropped = 0
    duplicated = 0

    for candidate in candidates:
        unknown = sorted({seq for seq in candidate.evidence_seqs if seq not in citable})
        if unknown:
            dropped += 1
            notes.append(
                f"dropped {candidate.subject!r}: it cites {unknown}, which this "
                f"stretch did not show the model"
            )
            continue

        witness = witness_for(
            events, candidate.evidence_seqs, session_id=session_id, repo=repo
        )
        memory_candidate = MemoryCandidate(
            text=candidate.text,
            scope=MemoryScope.REPO,
            scope_ref=scope_ref,
            type=candidate.type,
            subject=candidate.subject,
            tenant_id=tenant,
            model_confidence_hint=candidate.model_confidence_hint,
            metadata={"distilledFrom": session_id},
        )
        hashed = content_hash_of(memory_candidate)
        if hashed in ids_by_hash:
            duplicated += 1
            notes.append(
                f"skipped {candidate.subject!r}: the same claim was already "
                f"distilled in this run as {ids_by_hash[hashed]}"
            )
            continue
        if dry_run:
            notes.append(
                f"would file {candidate.subject!r} as "
                f"{'active' if witness.is_recallable else 'proposed'} "
                f"({witness.kind})"
            )
            continue

        provenance = Provenance(
            kind=witness.kind,
            session_id=session.header.session_id,
            repo=repo,
            evidence_refs=witness.evidence,
        )
        try:
            if witness.is_recallable:
                memory_id = str(await memory.write(memory_candidate, provenance))
                written.append(memory_id)
            else:
                memory_id = str(
                    await memory.admit_provisional(memory_candidate, provenance)
                )
                proposed.append(memory_id)
        except ForgePilotError as exc:
            # The store's own rules refused it. One refused claim must not abort a
            # batch: the rest of the extraction is still good, and the refusal is
            # recorded where an operator will see it.
            dropped += 1
            notes.append(f"dropped {candidate.subject!r}: the store refused it: {exc}")
            continue

        ids_by_hash[hashed] = memory_id
        await session.append(
            EventKind.MEMORY_WRITE,
            {
                "memoryId": memory_id,
                "session": session_id,
                "scope": str(MemoryScope.REPO),
                "scopeRef": scope_ref,
                "tenant": tenant,
                "type": str(candidate.type),
                "subject": candidate.subject,
                "text": candidate.text,
                "kind": str(witness.kind),
                "status": "active" if witness.is_recallable else "proposed",
                "confidence": str(confidence_for(witness.kind, witness.evidence)),
                "evidenceSeqs": [str(ref) for ref in witness.evidence],
                "reason": witness.reason,
                "dryRun": False,
            },
            surface=SurfaceOp.LOG_ONLY,
        )

    watermark_after = watermark
    if not dry_run:
        await watermarks.set(session_id, upto)
        watermark_after = upto

    final = replace(
        attempt,
        watermark_after=watermark_after,
        candidates=len(candidates),
        written=len(written),
        provisional=len(proposed),
        duplicated=duplicated,
        dropped=dropped,
        memories=tuple(written + proposed),
        notes=tuple(notes),
        finished_at=datetime.now(UTC),
    )
    await session.append(
        EventKind.DISTILLATION_END, final.to_event_data(), surface=SurfaceOp.LOG_ONLY
    )
    await session.flush()
    return final


def evidence_digest(events: Sequence[SessionEvent], *, limit: int = EVIDENCE_DIGEST_CHARS) -> str:
    """A map of what may be cited: one line per citable event, seq first.

    Only the kinds that can carry trust are listed — a cited user message, a cited
    check, a cited tool result — plus a note about assistant messages, which carry
    none. The transcript is already in the request; this exists so the model can
    name a seq number instead of guessing one, which is the difference between a
    citation and an invention.
    """
    lines: list[str] = []
    truncated = 0
    for event in events:
        if not event.is_model_visible:
            continue
        line = _digest_line(event)
        if line is None:
            continue
        if sum(len(entry) + 1 for entry in lines) + len(line) > limit:
            truncated += 1
            continue
        lines.append(line)
    if not lines:
        return ""
    header = (
        "== Events you may cite, by seq ==\n"
        "These are the only seq numbers that will be accepted in evidence_seqs. A\n"
        "user message and a check that ran and passed carry trust; an assistant\n"
        "message carries none (it is the agent talking, not evidence).\n"
    )
    footer = ""
    if truncated:
        footer = (
            f"\n({truncated} further events are in the transcript but not listed "
            f"here; cite them only if you are certain of the number, because a "
            f"citation is checked.)\n"
        )
    return header + "\n".join(lines) + footer


def _digest_line(event: SessionEvent) -> str | None:
    kind = event.kind
    if kind == EventKind.USER_MESSAGE:
        return f"{event.seq} user: {_clip(str(event.data.get('text', '')))}"
    if kind == EventKind.TASK_CHECK:
        status = "COULD NOT RUN" if event.data.get("couldNotRun") else (
            "passed" if event.data.get("passed") else "failed"
        )
        command = _clip(str(event.data.get("command", "")) or "a check", limit=120)
        return f"{event.seq} check [{status}]: {command}"
    if kind == EventKind.TOOL_RESULT:
        failed = " ERROR" if event.data.get("isError") else ""
        return (
            f"{event.seq} tool result{failed}: "
            f"{_clip(str(event.data.get('content', '')))}"
        )
    if kind == EventKind.TASK_STATE:
        return (
            f"{event.seq} task state: "
            f"{_clip(str(event.data.get('state', event.data.get('status', ''))), limit=80)}"
        )
    return None


def _clip(text: str, *, limit: int = _DIGEST_LINE_CHARS) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def _closed_upto(events: Sequence[SessionEvent]) -> int:
    """The seq of the last event of the last closed turn, or 0."""
    last = 0
    for event in events:
        if event.kind == EventKind.TURN_END:
            last = max(last, event.seq)
    return last


def _timestamp(event: SessionEvent) -> datetime | None:
    stamp = event.ts
    if isinstance(stamp, datetime):
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    if isinstance(stamp, str) and stamp:
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


async def _fail(
    session: Session, report: DistillationReport, code: str, detail: str
) -> DistillationReport:
    """Record a run-level failure and close the run."""
    failed = replace(
        report, failed=code, detail=detail, finished_at=datetime.now(UTC)
    )
    await session.append(
        EventKind.DISTILLATION_FAILED,
        {"session": report.session_id, "code": code, "detail": detail},
        surface=SurfaceOp.LOG_ONLY,
    )
    await session.append(
        EventKind.DISTILLATION_END, failed.to_event_data(), surface=SurfaceOp.LOG_ONLY
    )
    await session.flush()
    return failed
