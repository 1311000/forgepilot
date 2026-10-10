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

__all__ = ["derive_messages", "build_request", "unsupported_event_kinds"]

#: Event kinds that become a message. Anything absent is log-only by construction
#: — a plugin can add events without accidentally feeding the model.
_SURFACE_KINDS = frozenset(
    {
        EventKind.USER_MESSAGE,
        EventKind.ASSISTANT_MESSAGE,
        EventKind.TOOL_RESULT,
        EventKind.TASK_CHECK,
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
                hits = [i for i, e in enumerate(visible) if low <= e.seq <= high]
                if hits:
                    visible = visible[: hits[0]] + [event] + visible[hits[-1] + 1 :]
                    placed = True
            if not placed:
                # Nothing matched: the replacement stands alone, at the end. That
                # is the honest place for a supersede range this projector cannot
                # find — the alternative would be to drop the message silently.
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
