"""Deriving a model request from the log.

**This is the only module that constructs a :class:`ChatRequest`.** That is the
mechanism behind *model-visible means logged*: every message the model receives is
produced here, from an event, carrying the ``source_event_seq`` that proves where
it came from. There is no other way to build a request, so there is no path that
could bypass the log — and :mod:`forgepilot.runtime.log_guard` checks the result.

The message list is **derived, never stored**. DSH makes the same choice: a
mutable ``messages[]`` alongside the log is a second source of truth, and the two
drift the first time anything edits one without the other.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from forgepilot.seams.llm import (
    ChatMessage,
    ChatRequest,
    Role,
    ToolCallRequest,
    ToolSchema,
)
from forgepilot.seams.refs import TurnId
from forgepilot.seams.session import EventKind, SessionEvent, SurfaceOp

__all__ = [
    "derive_messages",
    "build_request",
    "build_compaction_request",
    "build_distillation_request",
    "unsupported_event_kinds",
]

#: Event kinds that become a message. Anything absent is log-only by construction
#: — a plugin can add events without accidentally feeding the model.
_SURFACE_KINDS = frozenset(
    {
        EventKind.USER_MESSAGE,
        EventKind.ASSISTANT_MESSAGE,
        EventKind.TOOL_RESULT,
        EventKind.TASK_CHECK,
        # Carried on a `SurfaceOp.REPLACE`, so it appears in place of the span it
        # covers. A surface kind with no renderer raises rather than being
        # skipped, which is why the branch below is not optional.
        EventKind.COMPACTION_SUMMARY,
        # Encoded from the store by `runtime/recall.py` and carried as its own
        # `text`, so replaying the log rebuilds the same block the model saw.
        EventKind.MEMORY_RECALLED,
    }
)


def unsupported_event_kinds(events: Sequence[SessionEvent]) -> tuple[str, ...]:
    """Surface events this projection does not know how to render.

    Returned rather than ignored: an event marked surface that produces no
    message would mean the model is being shown a different history than the log
    says it saw.
    """
    return tuple(
        sorted(
            {
                event.kind
                for event in events
                if event.is_model_visible and event.kind not in _SURFACE_KINDS
            }
        )
    )


def _stands_within(event: SessionEvent, low: int, high: int) -> bool:
    """Whether ``event`` is currently what stands in for something in ``[low, high]``.

    Two ways to be, and the second is the one that was missing:

    - the event *is* one of those seqs — nothing has replaced it yet;
    - the event is itself a ``REPLACE`` whose own window overlaps the range, which
      is what is standing there now. Matching on ``seq`` alone cannot see that, so
      superseding an already-superseded range found no hits at all and fell through
      to the append branch.
    """
    if low <= event.seq <= high:
        return True
    if event.surface is not SurfaceOp.REPLACE:
        return False
    window = event.data.get("supersedes") or ()
    if not (isinstance(window, (list, tuple)) and len(window) == 2):
        return False
    inner_low, inner_high = int(window[0]), int(window[1])
    return inner_low <= high and low <= inner_high


def derive_messages(events: Sequence[SessionEvent]) -> tuple[ChatMessage, ...]:
    """Fold the log into the message list the model should see.

    Applies ``surface``: ``LOG_ONLY`` events are skipped, ``APPEND`` events join
    the end, and a ``REPLACE`` event supersedes the range named in its
    ``supersedes`` field (``[from, to]``, inclusive) — **taking that range's
    place**, so a mask or a summary stands where the history it collapsed stood.

    ⚠️ **In place, not appended, and the difference is not cosmetic.** The first
    version removed the superseded events and appended the replacement to the end
    of the visible list. For a whole-span summary that is nearly harmless, which
    is why it survived: the span is usually near the end. For masking a single
    tool result it is fatal. Measured on a two-pair conversation, masking only the
    first result:

        assistant calls=['c1']      <- its result is not next
        assistant calls=['c2']
        tool    result_for='c2'
        tool    result_for='c1'     <- three positions late

    An assistant message whose ``tool_calls`` is not answered immediately after it
    is rejected outright by the endpoint — **one turn later**, with a traceback
    pointing at the request rather than at whatever arranged the messages.

    The block replaced runs from the first matching event to the last, inclusive.
    Anything sitting *between* them belongs to the superseded range too: an
    element's position in this list follows its own original's position, so a
    replacement nested inside the range is inside the range.
    """
    visible: list[SessionEvent] = []

    for event in events:
        if not event.is_model_visible:
            continue
        if event.surface is SurfaceOp.REPLACE:
            window = event.data.get("supersedes") or []
            placed = False
            if isinstance(window, (list, tuple)) and len(window) == 2:
                low, high = int(window[0]), int(window[1])
                hits = [i for i, e in enumerate(visible) if _stands_within(e, low, high)]
                if hits:
                    visible = visible[: hits[0]] + [event] + visible[hits[-1] + 1 :]
                    placed = True
            if not placed:
                # ⚠️ **A replacement that cannot be placed is dropped when it
                # carries a call id**, and appended only when it does not.
                #
                # Appending is right for a summary: a handoff over a region is new
                # content, and the end is where new content goes. It is wrong for a
                # masked tool result, because that renders as a `tool` message — and
                # a `tool` message at the end of a conversation answers a call from
                # the start of it. The endpoint rejects the entire request:
                #
                #     Messages with role 'tool' must be a response to a preceding
                #     message with 'tool_calls'
                #
                # Dropping fails closed. The model then sees whatever still stands
                # in that range — the original body, unmasked — which is *more* than
                # the log promised, never less, so it cannot break *model-visible
                # means logged* in the direction that matters.
                #
                # This is a backstop, not the fix: `superseded_seqs` is what stops
                # the unplaceable replacement from being emitted at all. It is here
                # because "this should not happen" is the exact reasoning that let
                # the first one happen, and a conversation is not the place to find
                # out.
                if event.data.get("callId"):
                    continue
                visible.append(event)
            continue
        visible.append(event)

    messages: list[ChatMessage] = []
    for event in visible:
        message = _render(event)
        if message is not None:
            messages.append(message)
    return tuple(messages)


def _render(event: SessionEvent) -> ChatMessage | None:
    data = event.data
    if event.kind == EventKind.USER_MESSAGE:
        return ChatMessage(
            role=Role.USER,
            content=str(data.get("text", "")),
            source_event_seq=event.seq,
        )

    if event.kind == EventKind.ASSISTANT_MESSAGE:
        calls = tuple(
            ToolCallRequest(
                call_id=str(call.get("callId", "")),
                name=str(call.get("name", "")),
                arguments_json=str(call.get("arguments", "")),
            )
            for call in data.get("toolCalls", ())
        )
        text = data.get("text")
        return ChatMessage(
            role=Role.ASSISTANT,
            content=str(text) if text else None,
            tool_calls=calls,
            source_event_seq=event.seq,
        )

    if event.kind == EventKind.TOOL_RESULT:
        return ChatMessage(
            role=Role.TOOL,
            content=str(data.get("content", "")),
            tool_call_id=str(data.get("callId", "")),
            source_event_seq=event.seq,
        )

    if event.kind == EventKind.TASK_CHECK:
        # Rendered as a *user* message, not a tool result: the model did not ask
        # for this check, and dressing it as one would both misrepresent the log
        # and produce an orphan tool message the endpoint would reject.
        status = "passed" if data.get("passed") else "FAILED"
        command = data.get("command", "")
        output = str(data.get("output", "")).strip()
        body = [
            f"The harness ran this project's checks: {status}.",
            f"Command: {command}",
        ]
        if output:
            body.append("Output:")
            body.append(output)
        return ChatMessage(
            role=Role.USER,
            content="\n".join(body),
            source_event_seq=event.seq,
        )

    if event.kind == EventKind.COMPACTION_SUMMARY:
        # A *user* message, like the check above and for a related reason: this is
        # the harness telling the model what it needs to know about history it can
        # no longer see, not something the model said. Dressing it as an assistant
        # turn would attribute the summary to the model and invite it to continue
        # from words it never wrote.
        body = str(data.get("text", "")).strip()
        covered = data.get("supersedes")
        span = ""
        if isinstance(covered, (list, tuple)) and len(covered) == 2:
            # Named, so the model knows *that* it is looking at a compression and
            # roughly where — and so an auditor reading the conversation can find
            # the events it stands in for.
            span = f" (covering events {covered[0]}-{covered[1]})"
        return ChatMessage(
            role=Role.USER,
            content=f"[compacted conversation{span}]\n{body}",
            source_event_seq=event.seq,
        )

    if event.kind == EventKind.MEMORY_RECALLED:
        # A *user* message, like the two above: the harness is speaking. The body
        # is recorded in the event verbatim rather than re-rendered from the ids,
        # because a memory can be revoked, expired or superseded between the recall
        # and a later replay — and "what the model saw" must not change retroactively
        # when the store does.
        return ChatMessage(
            role=Role.USER,
            content=str(data.get("text", "")),
            source_event_seq=event.seq,
        )

    return None


async def build_request(
    session: Any,
    *,
    model: str,
    system_sections: Sequence[str],
    turn: TurnId,
    step: int,
    tools: Sequence[ToolSchema] = (),
    max_tokens: int = 4096,
    temperature: float = 0.0,
    metadata: Mapping[str, str] | None = None,
) -> ChatRequest:
    """Record the request header and derive the request from the log.

    The header is written **first and log-only**. The model never sees it as a
    message, but it is what makes the request reconstructable from the log alone —
    so it has to be recorded before dispatch, not summarised afterwards.
    """
    unsupported = unsupported_event_kinds(session.events)
    if unsupported:
        raise ValueError(
            f"surface events {list(unsupported)} have no rendering, so the model would "
            f"see less than the log says it saw. Either render them in projection.py or "
            f"mark them log-only."
        )

    await session.request_header(
        turn,
        step,
        {
            "model": model,
            "system": list(system_sections),
            "tools": [schema.name for schema in tools],
            "maxTokens": max_tokens,
            "temperature": temperature,
        },
    )

    return ChatRequest(
        model=model,
        system=tuple(system_sections),
        messages=derive_messages(session.events),
        tools=tuple(tools),
        max_tokens=max_tokens,
        temperature=temperature,
        metadata=dict(metadata or {}),
    )


async def build_compaction_request(
    session: Any,
    *,
    region_events: Sequence[SessionEvent],
    model: str,
    instruction: str,
    turn: TurnId,
    step: int,
    max_tokens: int,
) -> ChatRequest:
    """The request that asks a model to summarise a span of history.

    It lives **here** rather than in the compaction provider so that the claim
    "`projection.py` is the only module that constructs a `ChatRequest`" stays
    true for this path. That claim is already false in two places — the
    classifier and the review narrative each build their own, and neither is
    logged — and compaction is not going to become a third.

    The consequence is that this call is **guarded like any other**: it goes
    through `guarded_complete`, so it flushes first and every message it sends
    must carry a `source_event_seq` the log already contains. It does, because
    the messages are `derive_messages(region_events)` and every one of those
    events is already in the log. Nothing new is being shown to a model, and
    nothing new needs an exemption.

    `compaction/request` is written first and log-only, exactly as
    `request/header` is — that is what makes the one model call compaction makes
    reconstructable from the log afterwards.
    """
    await session.append(
        EventKind.COMPACTION_REQUEST,
        {
            "model": model,
            "instruction": instruction,
            "turn": int(turn),
            "step": step,
            "region": [region_events[0].seq, region_events[-1].seq] if region_events else [],
            "maxTokens": max_tokens,
        },
        surface=SurfaceOp.LOG_ONLY,
        turn=turn,
        step=step,
    )

    return ChatRequest(
        model=model,
        system=(instruction,),
        messages=derive_messages(region_events),
        max_tokens=max_tokens,
        temperature=0.0,
    )


async def build_distillation_request(
    session: Any,
    *,
    events: Sequence[SessionEvent],
    model: str,
    instruction: str,
    extra_system: Sequence[str] = (),
    limits: tuple[int, int] = (0, 0),
    max_tokens: int = 4096,
) -> ChatRequest:
    """The request that asks a model to extract memories from a stretch of log.

    For the same reason as :func:`build_compaction_request`, the request is built
    here and not in `runtime/distillation.py`: the messages must be
    `derive_messages(events)` over events the log already holds, so the call is
    guardable — every message carries a `source_event_seq` that is already durable.
    A distiller that assembled its own prompt could show the model something the
    log never held, which is precisely the invariant this module exists to keep.

    `extra_system` carries the citation digest: a map of the seq numbers the model
    is allowed to cite. It is a *second* system section rather than part of the
    transcript, because it is an instruction about the answer, not evidence —
    putting it among the messages would invite citing it.

    `distillation/request` is written first and log-only, like `request/header` and
    `compaction/request`, which is what makes the extraction reconstructable: the
    prompt, the model, the stretch `[watermark, upto]` and the token cap are all in
    the log before the call is made.
    """
    await session.append(
        EventKind.DISTILLATION_REQUEST,
        {
            "model": model,
            "instruction": instruction,
            "watermark": limits[0],
            "upto": limits[1],
            "region": [events[0].seq, events[-1].seq] if events else [],
            "events": len(events),
            "systemSections": 1 + len(extra_system),
            "maxTokens": max_tokens,
        },
        surface=SurfaceOp.LOG_ONLY,
    )

    return ChatRequest(
        model=model,
        system=(instruction, *extra_system),
        messages=derive_messages(events),
        max_tokens=max_tokens,
        temperature=0.0,
    )
