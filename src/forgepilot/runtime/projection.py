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
    {EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE, EventKind.TOOL_RESULT}
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
    the end, and ``REPLACE`` events supersede the range named in their
    ``supersedes`` field (``[from, to]``, inclusive) so a compaction summary can
    stand in for the history it collapsed.
    """
    visible: list[SessionEvent] = []

    for event in events:
        if not event.is_model_visible:
            continue
        if event.surface is SurfaceOp.REPLACE:
            window = event.data.get("supersedes") or []
            if isinstance(window, (list, tuple)) and len(window) == 2:
                low, high = int(window[0]), int(window[1])
                visible = [e for e in visible if not (low <= e.seq <= high)]
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
