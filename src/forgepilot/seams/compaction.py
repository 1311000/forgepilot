"""Context compaction — service name ``compaction``.

The agent loop is stateless by design: every step re-derives its request from the
append-only session log, so the conversation grows monotonically and eventually
stops fitting the model's window. Until this seam existed, that was the end of the
run — `runtime/loop/step.py` raised `ContextWindowExceeded` and its own message
said *"Compaction is not implemented yet"*.

**A note on the word.** This is not summarisation, and summarisation is not what
makes it work. The cheapest tier replaces the *bodies* of old tool results with a
short placeholder and calls no model at all; the evidence from every coding agent
that has published on this is that observation masking alone halves cost while
matching or beating LLM summarisation on solve rate, and tool output is roughly
84% of what grows. Summarisation is the second tier, for when masking is not
enough. **A summary of a summary is never taken** — errors compound.

Two properties are load-bearing, and both come from the log being append-only:

- **Compaction is a log write, not a log edit.** A pass appends events; the
  history it covers stays in the log, byte for byte. "Compaction-instead-of-
  mutation" has been in `CLAUDE.md` since M3 as a concept adopted from DSH; this
  is where it becomes a mechanism. Shadowed events remain greppable, which is
  what makes a run that went wrong still diagnosable afterwards.
- **What the model sees stays derivable from the log.** A pass never touches a
  message list — it emits a `SurfaceOp.REPLACE` event, and
  `projection.derive_messages` folds it. *Model-visible means logged* therefore
  holds through a compaction exactly as it holds everywhere else.

The seam is deliberately named in the plural-ish sense of *the decision to
compact*: what a provider decides changes, how it decides does not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from forgepilot.seams.refs import TurnId

__all__ = [
    "SERVICE_NAME",
    "CompactionTier",
    "CompactionPolicy",
    "CompactionOutcome",
    "CompactionService",
    "FAILURE_EMPTY",
    "FAILURE_TRUNCATED",
    "FAILURE_MODEL_ERROR",
    "FAILURE_NO_REGION",
    "FAILURE_CIRCUIT_OPEN",
    "FAILURE_CODES",
]

SERVICE_NAME = "compaction"


class CompactionTier(StrEnum):
    """Which tier acted. ``NONE`` means nothing was over the threshold."""

    NONE = "none"
    #: Replaced old tool-result bodies with placeholders. No model call.
    MASK = "mask"
    #: Replaced a span of whole turns with a model-written handoff.
    SUMMARY = "summary"


#: Why a pass did not happen. **Recorded, never raised** — a compaction that
#: cannot act is not an error in the run; it just means the run is on its own
#: from here, and the loud failure at the end is unchanged. DSH's six documented
#: codes are the shape this follows.
FAILURE_EMPTY = "empty-summary"
FAILURE_TRUNCATED = "summary-truncated"
FAILURE_MODEL_ERROR = "model-error"
FAILURE_NO_REGION = "no-eligible-region"
FAILURE_CIRCUIT_OPEN = "circuit-open"

FAILURE_CODES = frozenset(
    {FAILURE_EMPTY, FAILURE_TRUNCATED, FAILURE_MODEL_ERROR, FAILURE_NO_REGION, FAILURE_CIRCUIT_OPEN}
)


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """When to compact, how much, and what to leave alone.

    Defaults err toward *doing less*: a run that compacts sooner than it needed
    to has spent a little and lost a little, while a run that compacts too late
    has already failed.
    """

    enabled: bool = True
    #: Fraction of the window at which a pass fires. The research band is 60-93%
    #: across tools, trending earlier than the defaults were; 0.8 sits in it.
    trigger_fraction: float = 0.8
    #: Absolute room reserved for this step's response and the next one's growth.
    #: Whichever of the two conditions fires first wins.
    reserve_tokens: int = 8192
    #: Never compact the newest N turns. The model's foothold on "where am I".
    keep_recent_turns: int = 2
    #: Never mask the newest N tool results, for the same reason.
    keep_recent_tool_results: int = 4
    #: The first user message is the ticket. Compacting it away leaves a task
    #: with no statement of what it is for.
    pin_first_user_message: bool = True
    #: The most recent `task/check` is never covered — the repair loop's retry
    #: prompt tells the model to read the check output "above".
    pin_latest_check: bool = True
    tiers: tuple[CompactionTier, ...] = (CompactionTier.MASK, CompactionTier.SUMMARY)
    summary_max_tokens: int = 2048
    #: Refuse to summarise a span smaller than this: the summary would cost about
    #: what it saves.
    summary_min_region_tokens: int = 2000
    #: Consecutive failures before this session stops trying. Without it a run
    #: that cannot get under the threshold compacts, fails to, and compacts again
    #: — thrashing, which is worse than the failure it is trying to avoid.
    failure_limit: int = 3


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    """What a pass did, or why it did not."""

    performed: bool = False
    tier: CompactionTier = CompactionTier.NONE
    #: Inclusive seq range the pass covered, when it covered one.
    superseded: tuple[int, int] | None = None
    #: How many events left the model-visible surface. Reported rather than
    #: inferred, because "it ran" and "it freed something" are different claims.
    shadowed: int = 0
    estimated_before: int = 0
    estimated_after: int = 0
    failure: str = ""
    detail: str = ""

    @property
    def freed(self) -> int:
        return max(0, self.estimated_before - self.estimated_after)


@runtime_checkable
class CompactionService(Protocol):
    """Decides whether to compact the conversation, and does it.

    Takes the session because a pass *writes* to the log — it does not return a
    message list, and it must not. A provider that returned messages would put a
    second constructor of the conversation outside `runtime/projection.py`, which
    is the module the whole *model-visible means logged* argument rests on.

    ``estimated_tokens`` is supplied by the caller so the trigger is measured
    where the request is built, and so a policy can be tested without a model.
    """

    def policy(self) -> CompactionPolicy: ...

    async def compact(
        self,
        *,
        session: Any,
        llm: Any,
        model: str,
        system_sections: Sequence[str],
        turn: TurnId,
        step: int,
        estimated_tokens: int,
        window_tokens: int,
    ) -> CompactionOutcome: ...
