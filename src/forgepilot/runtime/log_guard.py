"""Enforcement of *model-visible means logged*.

The invariant is only meaningful if violating it **fails**. A warning would be
read past; a log line would be missed. So a request that contains anything the
log cannot vouch for raises, and the step dies.

Two things are checked, and they are the same concern seen twice:

1. Every message carries a ``source_event_seq`` that the log actually contains.
   A message with no provenance reached the model from somewhere other than the
   session log.
2. The session has nothing buffered. Sending a request while events are still in
   memory would mean a crash could lose what the model was told — the log would
   then disagree with reality, which is exactly the state the invariant forbids.

:func:`guarded_stream` performs the flush itself rather than documenting that
callers should, because the rule "remember to flush first" is one that will
eventually be forgotten in a new code path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from forgepilot.kernel.errors import ModelVisibleNotLogged
from forgepilot.seams.llm import ChatRequest, ChatResponse, StreamChunk

__all__ = ["assert_logged", "guarded_stream", "guarded_complete"]


def assert_logged(session: Any, request: ChatRequest) -> None:
    """Raise unless every part of ``request`` is already in the session log."""
    durable = session.durable_seq

    for index, message in enumerate(request.messages):
        if message.source_event_seq is None:
            raise ModelVisibleNotLogged(
                f"message {index} (role={message.role}) has no source_event_seq, so it "
                f"did not come from the session log. Every model input must be derived "
                f"from a logged event — see runtime/projection.py, which is the only "
                f"module that may build a request."
            )
        if message.source_event_seq > durable:
            raise ModelVisibleNotLogged(
                f"message {index} cites event {message.source_event_seq}, but only "
                f"{durable} events are durable. The model would see something the log "
                f"does not yet contain."
            )

    if session.has_pending:
        raise ModelVisibleNotLogged(
            f"{len(session._pending)} event(s) are buffered but not written. Sending a "
            f"request now would mean a crash loses what the model was told. Call "
            f"session.flush() — or use guarded_stream(), which does it for you."
        )


async def guarded_stream(
    session: Any, llm: Any, request: ChatRequest
) -> AsyncIterator[StreamChunk]:
    """Flush, verify the invariant, then stream.

    Use this rather than calling the model directly: it is the point where the
    invariant stops being a rule and becomes a property of the code path.
    """
    await session.flush()
    assert_logged(session, request)
    async for chunk in llm.stream(request):
        yield chunk


async def guarded_complete(session: Any, llm: Any, request: ChatRequest) -> ChatResponse:
    """Non-streaming counterpart to :func:`guarded_stream`."""
    await session.flush()
    assert_logged(session, request)
    return await llm.complete(request)
