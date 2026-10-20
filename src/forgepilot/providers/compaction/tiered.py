"""Compaction, cheapest tier first — service ``compaction``.

The order is the design, and it is not a preference. Every coding agent that has
published on this converges on the same finding: **replacing the bodies of old
tool results costs nothing and does most of the work**, because tool output is
roughly 84% of what grows a coding conversation. Summarisation is what you reach
for when that is not enough, and it is the tier that can go wrong — it costs a
model call, it can be truncated, and its errors compound if you ever summarise a
summary.

So:

1. **Mask.** One `SurfaceOp.REPLACE` per old tool result, carrying a short
   placeholder instead of the body. No model call. The assistant messages are
   **untouched**, which is what makes it structurally impossible for this tier to
   separate a tool call from its answer — the thing that breaks other agents and
   reports itself one turn late.
2. **Summarise.** Whole turns, replaced by a model-written handoff. Bounded by
   pinned prefixes, a recent tail, and a barrier at the last summary.

A pass that cannot get under the threshold says so and appends nothing that would
hide history. The loud `ContextWindowExceeded` at the top of the loop is
unchanged and is still the last word — compaction narrows the cases that reach it,
it does not replace it.

**What this provider never does: touch a message list.** It appends events and
lets `projection.derive_messages` fold them. That is what keeps *model-visible
means logged* true through a compaction, and it is why the one model call it makes
goes through `build_compaction_request` in `runtime/projection.py` rather than
building its own `ChatRequest` here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.runtime.context_size import estimate_tokens, measure
from forgepilot.runtime.events import (
    first_user_message_seq,
    last_seq_of_kind,
    superseded_seqs,
    turn_spans,
)
from forgepilot.runtime.log_guard import guarded_complete
from forgepilot.runtime.projection import build_compaction_request
from forgepilot.seams.compaction import (
    FAILURE_CIRCUIT_OPEN,
    FAILURE_EMPTY,
    FAILURE_MODEL_ERROR,
    FAILURE_NO_REGION,
    FAILURE_TRUNCATED,
    SERVICE_NAME,
    CompactionOutcome,
    CompactionPolicy,
    CompactionTier,
)
from forgepilot.seams.session import EventKind, SurfaceOp

__all__ = [
    "TieredCompactionPlugin",
    "SUMMARY_INSTRUCTION",
    "COSTLY_FAILURES",
    "consecutive_failures",
    "mask_placeholder",
    "select_mask_targets",
    "select_summary_region",
    "snap_to_pairs",
]

#: The handoff the model is asked to write. Sectioned rather than free-form
#: because every tool that has published one converged on this shape, and because
#: a summary that omits "why" produces an agent that re-litigates settled
#: decisions. `<analysis>` is scratch and is discarded — only `<summary>` is kept.
SUMMARY_INSTRUCTION = """\
You are compacting a coding agent's conversation so it can keep working in a
smaller context window. The conversation you are shown is about to be replaced by
what you write, so anything you leave out is gone.

Work inside <analysis></analysis> first — identify the objective, the technical
details that matter, what is relevant to what comes next. That part is discarded.

Then write the handoff inside <summary></summary>, using exactly these sections.
Write "None." under a section that does not apply rather than omitting it.

## Objective
What the user is trying to accomplish, and any refinement they have made to it.

## Key decisions and why
Each decision with the reason behind it. **Never drop the why** — a decision
without its reason gets undone by the next step.

## Files and symbols
Every file path, function, class and line number that has been read, written or
discussed. Exact names, not descriptions.

## Commands and results
Each command with its outcome. Keep error text verbatim when it is still relevant.

## Open problems
Each error with its status. For anything unresolved: what was last tried, and
what to try next. Do not report something as fixed unless a check passed.

## Working state
The exact state of the workspace when this snapshot was taken.

## Next step
The single next action, with a direct quote from the user if one constrains it.
"""

#: How much of a masked result's body survives. Enough to recognise the file and
#: the shape of what came back; not enough to be worth compacting again.
_HEAD = 80
_TAIL = 40

#: The failures the breaker counts: the ones that consumed a model call.
#: `no-eligible-region` is deliberately absent — see `consecutive_failures`.
COSTLY_FAILURES = frozenset({FAILURE_MODEL_ERROR, FAILURE_TRUNCATED, FAILURE_EMPTY})


# ---------------------------------------------------------------------------
# Pure helpers — region selection
# ---------------------------------------------------------------------------
def _surface_events(events: Sequence[Any]) -> list[Any]:
    return [e for e in events if e.is_model_visible]


def _resummarise_barrier(events: Sequence[Any]) -> int:
    """Seq of the last summary. **Nothing at or below it is compacted again.**

    This is the barrier, and it is deliberately drawn at the last *summary*
    rather than at the last `REPLACE`. A mask placeholder is not a summary — it
    is a note that a body was elided — and refusing to summarise a region because
    it contains one would stop the two tiers composing at all. A summary of a
    summary is the thing that compounds errors, and that is what this stops.
    """
    return last_seq_of_kind(events, EventKind.COMPACTION_SUMMARY) or 0


def _protected_seqs(events: Sequence[Any], policy: CompactionPolicy) -> frozenset[int]:
    """Seqs no pass may cover, whatever else the policy says."""
    protected: set[int] = set()
    if policy.pin_first_user_message:
        first = first_user_message_seq(events)
        if first is not None:
            protected.add(first)
    if policy.pin_latest_check:
        # The repair loop's retry prompt tells the model to read the check output
        # "above". Compacting it away leaves the prompt pointing at nothing.
        check = last_seq_of_kind(events, EventKind.TASK_CHECK)
        if check is not None:
            protected.add(check)
    return frozenset(protected)


def mask_placeholder(name: str, content: str) -> str:
    """What stands in for a tool result's body.

    Keeps the tool's name, its size and both ends of the text. The point is that
    the model can still tell *what it did and roughly what came back* — and that
    re-fetching is possible, because `read_file` and `code_search` are still
    there and the path is usually in the head.
    """
    body = content or ""
    if len(body) <= _HEAD + _TAIL + 40:
        return body
    head = body[:_HEAD].replace("\n", " ")
    tail = body[-_TAIL:].replace("\n", " ")
    return f"[tool output elided: {name}, {len(body)} bytes]\n  head: {head}\n  tail: {tail}"


def select_mask_targets(events: Sequence[Any], policy: CompactionPolicy) -> tuple[int, ...]:
    """Which tool results to mask, oldest first.

    ⚠️ **`surface is APPEND` is not enough, and that was the defect.** The
    docstring here used to claim that "a placeholder carries `REPLACE`, so a
    second pass cannot mask a mask". The placeholder does; the *result it replaced*
    does not, and the result it replaced is what this walk sees — the log is
    append-only and a `REPLACE` supersedes without mutating. So a second pass
    re-selected every result the first pass had masked.

    `superseded_seqs` is the missing question: *has something already taken this
    event's place?* A pass that asks it finds nothing to do on the second run,
    which is both correct and free.

    The newest ``keep_recent_tool_results`` are left alone: they are what the model
    is currently reasoning about.
    """
    barrier = _resummarise_barrier(events)
    protected = _protected_seqs(events, policy)
    already = superseded_seqs(events)

    candidates = [
        event.seq
        for event in events
        if event.kind == EventKind.TOOL_RESULT
        and event.surface is SurfaceOp.APPEND
        and event.seq > barrier
        and event.seq not in protected
        and event.seq not in already
    ]
    if policy.keep_recent_tool_results:
        candidates = candidates[: -policy.keep_recent_tool_results] or []
    return tuple(candidates)


def snap_to_pairs(events: Sequence[Any], low: int, high: int) -> tuple[int, int]:
    """Widen ``[low, high]`` until no tool call inside is answered outside.

    Compaction works in whole turns, and a turn contains its own calls and
    results — so this is normally a no-op. It is here because the failure it
    prevents is severe and asymmetric: an assistant message whose ``tool_calls``
    is not answered immediately after it is rejected outright by the endpoint, and
    the error arrives **one turn later**, with a traceback pointing at the request
    rather than at the cut that caused it.
    """
    calls: dict[str, int] = {}
    results: dict[str, int] = {}
    for event in events:
        call_id = event.data.get("callId")
        if not call_id:
            continue
        if event.kind == EventKind.TOOL_CALL:
            calls[str(call_id)] = event.seq
        elif event.kind == EventKind.TOOL_RESULT:
            results[str(call_id)] = event.seq

    changed = True
    while changed:
        changed = False
        for call_id, call_seq in calls.items():
            result_seq = results.get(call_id)
            if result_seq is None:
                continue
            if (low <= call_seq <= high) != (low <= result_seq <= high):
                low, high = min(low, call_seq), max(high, result_seq)
                changed = True
    return low, high


def select_summary_region(
    events: Sequence[Any], policy: CompactionPolicy
) -> tuple[int, int] | None:
    """The span to summarise, or ``None`` when there is nothing eligible.

    Built from **whole completed turns**, which is the smallest unit whose
    boundaries are already meaningful. A span that starts mid-turn can cut a call
    away from its result; a span that covers the newest turns takes away the thing
    the model is working on.
    """
    barrier = _resummarise_barrier(events)
    protected = _protected_seqs(events, policy)

    eligible = [
        span
        for span in turn_spans(events)
        if span.complete
        and span.end_seq > barrier
        # ⚠️ **Containment, not just the boundaries.** Checking only
        # `start_seq`/`end_seq` against the protected set looks right and is not:
        # the brief is a `user/message` in the middle of its own turn, so a turn
        # whose *ends* are unprotected can still contain it — and the first
        # version did exactly that, summarising the ticket away while every
        # boundary check passed.
        and not any(span.start_seq <= seq <= span.end_seq for seq in protected)
    ]
    if policy.keep_recent_turns:
        eligible = eligible[: -policy.keep_recent_turns] or []
    if not eligible:
        return None

    low = eligible[0].start_seq
    high = eligible[-1].end_seq
    return snap_to_pairs(events, low, high)


def consecutive_failures(events: Sequence[Any]) -> int:
    """Passes that failed since the last one that worked.

    **Read from the log, not kept in memory.** A compactor is reconstructed per
    session and per turn, so an in-memory counter resets on resume and the
    breaker silently reopens — which is the exact case it exists for, because a
    resumed session is the one most likely to be near its window.

    ⚠️ **Only failures that cost a model call count, and the distinction is the
    breaker's whole point.** The breaker exists to stop *spending*, so a pass that
    spent nothing is not evidence of thrash. `no-eligible-region` is the code that
    does not spend — and it is precisely the code that resolves itself: a region
    below the summary floor grows past it as the conversation does, and a fully
    masked conversation keeps producing new results to mask.

    Counting those latched the breaker **permanently** on a session that was about
    to become compactable, because it cannot unlatch: it blocks the very pass that
    would record a success and reset the streak. Observed on every step of a real
    `tight-window` run.
    """
    count = 0
    for event in reversed(events):
        if event.kind == EventKind.COMPACTION_FAILED:
            if str(event.data.get("code", "")) in COSTLY_FAILURES:
                count += 1
        elif event.kind in (EventKind.COMPACTION_SUMMARY, EventKind.COMPACTION_MASK):
            break
    return count


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------
class TieredCompactionPlugin(PluginService):
    """Serves ``compaction``; masks first, summarises second.

    Config: every field of :class:`~forgepilot.seams.compaction.CompactionPolicy`.
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})

        # An unknown key raises rather than being ignored. A misspelled
        # `trigger_fraction` would otherwise leave the default in place and the
        # deployment would believe it had changed something — the same shape as
        # every field this project has found that looked enforced and was not.
        known = set(CompactionPolicy.__dataclass_fields__)
        unknown = sorted(set(cfg) - known)
        if unknown:
            raise ValueError(f"unknown compaction config key(s) {unknown}. Known: {sorted(known)}")

        kwargs: dict[str, Any] = {k: v for k, v in cfg.items() if k != "tiers"}
        if cfg.get("tiers") is not None:
            kwargs["tiers"] = tuple(CompactionTier(str(t)) for t in cfg["tiers"])
        self._policy = CompactionPolicy(**kwargs)

    def policy(self) -> CompactionPolicy:
        return self._policy

    # -- trigger -----------------------------------------------------------
    @staticmethod
    def _over_threshold(estimated: int, window: int, policy: CompactionPolicy) -> bool:
        """Two conditions, **whichever fires first**.

        The absolute reserve is the one that matters on a small window: a
        percentage alone leaves no room for the reply that is about to be asked
        for. The fraction is the one that matters on a large window, where a fixed
        reserve is noise.
        """
        return (
            estimated + policy.reserve_tokens >= window
            or estimated >= window * policy.trigger_fraction
        )

    def _nothing(self, estimated: int, *, failure: str = "", detail: str = "") -> CompactionOutcome:
        return CompactionOutcome(
            performed=False,
            estimated_before=estimated,
            estimated_after=estimated,
            failure=failure,
            detail=detail,
        )

    # -- the pass ----------------------------------------------------------
    async def compact(
        self,
        *,
        session: Any,
        llm: Any,
        model: str,
        system_sections: Sequence[str],
        turn: Any,
        step: int,
        estimated_tokens: int,
        window_tokens: int,
    ) -> CompactionOutcome:
        policy = self._policy
        if not policy.enabled or not policy.tiers:
            return self._nothing(estimated_tokens, detail="compaction is disabled")
        if not self._over_threshold(estimated_tokens, window_tokens, policy):
            return self._nothing(estimated_tokens)
        if consecutive_failures(session.events) >= policy.failure_limit:
            return self._nothing(
                estimated_tokens,
                failure=FAILURE_CIRCUIT_OPEN,
                detail=(
                    f"{policy.failure_limit} consecutive passes failed to bring this "
                    f"session under the window; not trying again. The step will fail "
                    f"loudly rather than thrash."
                ),
            )

        # The lock, **flushed on its own** before anything else. A crash from here
        # on leaves an unmatched `compaction/start`, which the next pass can see —
        # two compactions over one session would interleave their replacements.
        await session.append(
            EventKind.COMPACTION_START,
            {
                "turn": int(turn),
                "step": step,
                "estimated": estimated_tokens,
                "window": window_tokens,
            },
            turn=turn,
            step=step,
        )
        await session.flush()

        acted = CompactionTier.NONE
        shadowed = 0

        if CompactionTier.MASK in policy.tiers:
            shadowed += await self._mask(session, turn, step)
            if shadowed:
                acted = CompactionTier.MASK
                await session.flush()
                now = measure(session.events, system_sections)
                if not self._over_threshold(now, window_tokens, policy):
                    await self._end(session, turn, step, CompactionTier.MASK, shadowed, "")
                    return CompactionOutcome(
                        performed=True,
                        tier=CompactionTier.MASK,
                        shadowed=shadowed,
                        estimated_before=estimated_tokens,
                        estimated_after=now,
                    )

        if CompactionTier.SUMMARY not in policy.tiers:
            detail = "masking was not enough and summarisation is not configured"
            await self._end(session, turn, step, acted, shadowed, detail)
            return CompactionOutcome(
                performed=bool(shadowed),
                tier=acted,
                shadowed=shadowed,
                estimated_before=estimated_tokens,
                estimated_after=measure(session.events, system_sections),
                failure=FAILURE_NO_REGION,
                detail=detail,
            )

        outcome = await self._summarise(
            session, llm, model, system_sections, turn, step, estimated_tokens, policy
        )
        # `replace`, not `__dict__` — `CompactionOutcome` is a slots dataclass and
        # has no `__dict__` at all.
        return replace(
            outcome,
            shadowed=shadowed + outcome.shadowed,
            tier=outcome.tier if outcome.tier is not CompactionTier.NONE else acted,
        )

    # -- tier 1 ------------------------------------------------------------
    async def _mask(self, session: Any, turn: Any, step: int) -> int:
        """Replace old tool-result bodies. Returns how many were shadowed."""
        events = session.events
        targets = select_mask_targets(events, self._policy)
        if not targets:
            return 0

        names = {
            str(e.data.get("callId", "")): str(e.data.get("name", "tool"))
            for e in events
            if e.kind == EventKind.TOOL_CALL
        }
        by_seq = {e.seq: e for e in events}

        masked: list[dict[str, Any]] = []
        for seq in targets:
            event = by_seq[seq]
            original = str(event.data.get("content", ""))
            placeholder = mask_placeholder(
                names.get(str(event.data.get("callId", "")), "tool"), original
            )
            if placeholder == original:
                # Nothing to save. Leaving it alone is better than writing an
                # event that changes nothing and counts as a pass.
                continue
            await session.append(
                EventKind.TOOL_RESULT,
                {
                    "callId": event.data.get("callId", ""),
                    "content": placeholder,
                    "isError": bool(event.data.get("isError")),
                    # The same call id, so the message renders as the *answer to
                    # the call it always answered* — which is what keeps the pair
                    # adjacent and the request valid.
                    "masked": True,
                    "originalBytes": len(original),
                    "supersedes": [seq, seq],
                },
                surface=SurfaceOp.REPLACE,
                turn=turn,
                step=step,
            )
            masked.append(
                {"seq": seq, "bytes": len(original), "saved": len(original) - len(placeholder)}
            )

        if not masked:
            return 0

        await session.append(
            EventKind.COMPACTION_MASK,
            {"count": len(masked), "results": masked},
            turn=turn,
            step=step,
        )
        return len(masked)

    # -- tier 2 ------------------------------------------------------------
    async def _summarise(
        self,
        session: Any,
        llm: Any,
        model: str,
        system_sections: Sequence[str],
        turn: Any,
        step: int,
        estimated_tokens: int,
        policy: CompactionPolicy,
    ) -> CompactionOutcome:
        events = session.events
        region = select_summary_region(events, policy)
        if region is None:
            detail = (
                "no span is eligible: everything left is either pinned, inside the "
                "recent tail, or already summarised"
            )
            await self._failed(session, turn, step, FAILURE_NO_REGION, detail)
            return self._nothing(estimated_tokens, failure=FAILURE_NO_REGION, detail=detail)

        low, high = region
        region_events = tuple(e for e in events if low <= e.seq <= high and e.is_model_visible)
        if not region_events:
            detail = "the selected span holds nothing the model can see"
            await self._failed(session, turn, step, FAILURE_NO_REGION, detail)
            return self._nothing(estimated_tokens, failure=FAILURE_NO_REGION, detail=detail)

        # A span this small would cost about what it saves. Measured against the
        # *region*, not against the region plus the system prompt — the prompt is
        # not being compacted and would swamp a small window's arithmetic.
        region_cost = build_request_messages(region_events)
        region_cost = estimate_tokens(tuple(region_cost), ())
        if region_cost < policy.summary_min_region_tokens:
            detail = (
                f"the span is about {region_cost} tokens, below the "
                f"{policy.summary_min_region_tokens} floor — summarising it would "
                f"cost what it saves"
            )
            await self._failed(session, turn, step, FAILURE_NO_REGION, detail)
            return self._nothing(estimated_tokens, failure=FAILURE_NO_REGION, detail=detail)

        request = await build_compaction_request(
            session,
            region_events=region_events,
            model=model,
            instruction=SUMMARY_INSTRUCTION,
            turn=turn,
            step=step,
            max_tokens=policy.summary_max_tokens,
        )
        try:
            response = await guarded_complete(session, llm, request)
        except Exception as exc:  # noqa: BLE001 - a summariser that breaks is not a summary
            detail = f"{type(exc).__name__}: {exc}"
            await self._failed(session, turn, step, FAILURE_MODEL_ERROR, detail)
            return self._nothing(estimated_tokens, failure=FAILURE_MODEL_ERROR, detail=detail)

        text = extract_summary(response.message.content or "")
        usage = getattr(response, "usage", None)
        produced = getattr(usage, "output_tokens", 0) or 0

        # **Fail closed.** A summary that hit the output cap is a *checkpoint cut
        # in half*, and every later step would reason from it. DSH's #3201 is
        # exactly this: the truncated checkpoint was kept, and it gutted the
        # session on resume. A missing summary costs a compaction; a truncated one
        # costs the run.
        #
        # The check is on the usage we asked for rather than on `stop_reason`,
        # because **no provider populates `stop_reason`** — it sits at its default
        # and a check on it would never fire. `stop_reason` is consulted too, so
        # that when someone does populate it this gets sharper on its own.
        truncated = produced >= policy.summary_max_tokens or getattr(
            response, "stop_reason", ""
        ) in {"length", "max_tokens"}
        if truncated:
            detail = (
                f"the summariser used its whole {policy.summary_max_tokens}-token budget, "
                f"so the handoff is cut off. Discarded rather than kept: a truncated "
                f"checkpoint is worse than none."
            )
            await self._failed(session, turn, step, FAILURE_TRUNCATED, detail)
            return self._nothing(estimated_tokens, failure=FAILURE_TRUNCATED, detail=detail)

        if not text.strip():
            detail = "the summariser returned nothing usable"
            await self._failed(session, turn, step, FAILURE_EMPTY, detail)
            return self._nothing(estimated_tokens, failure=FAILURE_EMPTY, detail=detail)

        await session.append(
            EventKind.COMPACTION_SUMMARY,
            {
                "text": text,
                "supersedes": [low, high],
                "model": model,
                "outputTokens": produced,
            },
            surface=SurfaceOp.REPLACE,
            turn=turn,
            step=step,
        )
        await session.flush()

        now = measure(session.events, system_sections)
        await self._end(session, turn, step, CompactionTier.SUMMARY, high - low + 1, "")
        return CompactionOutcome(
            performed=True,
            tier=CompactionTier.SUMMARY,
            superseded=(low, high),
            shadowed=high - low + 1,
            estimated_before=estimated_tokens,
            estimated_after=now,
        )

    # -- logging -----------------------------------------------------------
    async def _end(
        self, session: Any, turn: Any, step: int, tier: CompactionTier, shadowed: int, detail: str
    ) -> None:
        await session.append(
            EventKind.COMPACTION_END,
            {"tier": str(tier), "shadowed": shadowed, "detail": detail},
            turn=turn,
            step=step,
        )
        await session.flush()

    async def _failed(self, session: Any, turn: Any, step: int, code: str, detail: str) -> None:
        """Record a failed pass, then close it.

        **Failures are recorded, not raised.** A compaction that could not act is
        not an error in the run — it means the run is on its own from here, and
        the loud failure at the top of the loop still says so. What must not
        happen is a failure leaving no trace, because the breaker counts these.
        """
        await session.append(
            EventKind.COMPACTION_FAILED, {"code": code, "detail": detail}, turn=turn, step=step
        )
        await self._end(session, turn, step, CompactionTier.NONE, 0, f"{code}: {detail}")


def build_request_messages(region_events: Sequence[Any]) -> list[Any]:
    """The messages a span renders to — for measuring it, not for sending it."""
    from forgepilot.runtime.projection import derive_messages

    return list(derive_messages(region_events))


def extract_summary(text: str) -> str:
    """Keep `<summary>…</summary>`; discard `<analysis>`.

    The scratchpad is where the model works out what matters. Keeping it would
    spend the budget on reasoning that has already been done, and it is the part
    most likely to contain a wrong turn the model then believes.
    """
    lowered = text.lower()
    start = lowered.find("<summary>")
    end = lowered.rfind("</summary>")
    if start != -1 and end != -1 and end > start:
        return text[start + len("<summary>") : end].strip()
    # No tags: a model that ignored the instruction still wrote *something*, and
    # the alternative to keeping it is discarding a whole pass over formatting.
    return text.strip()
