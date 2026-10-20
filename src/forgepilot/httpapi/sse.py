"""Streaming a session's log to a browser.

**The stream is the log.** Not a summary of it, not a parallel event bus — the
same append-ordered events, carrying their ``seq`` as the SSE id. Three things
fall out of that choice, and each is a property worth having rather than a
coincidence:

- ``Last-Event-ID`` needs no invention. It is the log's ``seq``, so a reconnect
  is a ``read(after=...)``: the store already has that parameter.
- What the browser sees is what the log says happened, so the transport cannot
  drift from the record.
- Ordering is not the transport's to decide. ``approval/asked`` preceding
  ``tool/result`` is the log's ordering, and the UI shows it because it is true.

**Only durable events go on the wire.** Model token deltas are deliberately not
streamed: they are the one thing the log does not hold, and a stream carrying
both would make "is this history?" unanswerable from the client side. The cost is
that a long model step looks like silence until its ``assistant/message`` lands.
The compensation is that the *step* is visible — ``step/start`` arrives
immediately — so the UI can say "working" instead of appearing hung.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

__all__ = ["LostStream", "tail_session"]

#: How many events may be queued for one slow client before the stream gives up.
#: Bounded on purpose — see :class:`LostStream`.
QUEUE_LIMIT = 1024


class LostStream(Exception):
    """A client fell too far behind and its queue overflowed.

    Raised rather than dropping events quietly. A stream with a hole in it is
    worse than a closed one: the client cannot tell, and would render a
    confident, incomplete account of what happened. Closing the stream makes the
    client reconnect, and a reconnect replays the gap exactly, because the gap is
    in the log and the cursor is a sequence number.
    """


def frame(event: Any) -> dict[str, Any]:
    """One ``SessionEvent`` as an SSE frame.

    ``id`` is the event's ``seq`` — the same number the store orders by, so the
    cursor a client sends back is meaningful to storage and not just to us.
    """
    return {"id": str(event.seq), "event": event.kind, "data": _encode(event)}


def _encode(event: Any) -> str:
    payload = event.to_json() if hasattr(event, "to_json") else dict(event.data)
    return json.dumps(payload, ensure_ascii=False)


async def tail_session(
    session: Any,
    *,
    after: int = 0,
    queue_limit: int = QUEUE_LIMIT,
    stopping: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield frames for every event after ``after``, then keep yielding.

    The ordering below is load-bearing. The backlog is snapshotted **before**
    subscribing, and the subscription only delivers events past the backlog's
    high-water mark — subscribe-then-snapshot would double-deliver whatever
    arrived in the gap, and snapshot-then-subscribe alone would lose it. Sequence
    numbers make the two halves exactly separable, which is the dividend of
    having them be contiguous.

    ``stopping`` ends the stream on shutdown instead of leaving it parked on an
    empty queue until uvicorn's grace period cancels it. Ending is also the
    honest signal: the client reconnects, and the reconnect either reaches a
    restarted process (which replays from storage) or fails visibly. A stream
    that simply stops producing looks identical to one where nothing happened.
    """
    backlog = tuple(e for e in session.events if e.seq > after)
    delivered = max((e.seq for e in backlog), default=after)

    queue: asyncio.Queue = asyncio.Queue(maxsize=queue_limit)
    overflowed = False

    def listener(event: Any) -> None:
        # Runs inline on the writing task, so this must never block. A full queue
        # means the client is not keeping up.
        nonlocal overflowed
        if overflowed:
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            overflowed = True

    unsubscribe = session.subscribe(listener)
    try:
        for event in backlog:
            yield frame(event)

        while True:
            event = await _next(queue, stopping)
            if event is None:
                return
            if overflowed:
                raise LostStream(
                    f"client fell more than {queue_limit} events behind; reconnect "
                    f"with Last-Event-ID to replay the gap"
                )
            # Everything before `delivered` was already sent from the backlog.
            if event.seq <= delivered:
                continue
            delivered = event.seq
            yield frame(event)
    finally:
        unsubscribe()


async def _next(queue: asyncio.Queue, stopping: asyncio.Event | None) -> Any | None:
    """The next event, or ``None`` once shutdown has been asked for.

    Two futures rather than a polling loop, because a timeout would make the
    shutdown latency a number someone has to guess, and a sleep is the one thing
    this module has carefully avoided. Whichever finishes first wins; the other
    is cancelled and awaited so a cancelled ``queue.get`` cannot swallow the
    event it had already taken.
    """
    if stopping is None:
        return await queue.get()

    getter = asyncio.ensure_future(queue.get())
    stopper = asyncio.ensure_future(stopping.wait())
    try:
        done, _pending = await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (getter, stopper):
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001 - cancellation is expected here
                    pass
    if stopper in done:
        return None
    return getter.result()
