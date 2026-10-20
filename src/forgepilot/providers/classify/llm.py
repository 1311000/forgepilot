"""A classifier backed by the configured model — service ``classifier``.

The stub declines everything. That is the right *test double* — a keyword
heuristic pretending to classify would look like it worked and would blur the
distinction the seam exists to prove — but it leaves the whole task flow
unreachable outside its tests: `plan_task` sends anything with `needs_human` to
`AWAITING_INFO`, and a declining classifier sets that on every ticket. Something
has to actually classify.

**Every failure declines; none of them guesses.** The output of this provider
routes tools. A model that returns confident nonsense about a ticket would pick a
tool set on the strength of it — which is why the router can only ever *narrow*,
and why the second line of defence is that a malformed, low-confidence, or
incomplete answer becomes `needs_human` rather than a category.

**The ticket goes in the user turn.** A classifier copies ticket text into its
fields; if that text arrived in the system prompt it would be speaking with the
harness's voice and would arrive already privileged. This is the same split
`context_assembly` enforces for the task brief, and it is asserted by a test.

Configured with ``model`` and ``max_tokens``. It reads the model through the
``llm`` service, so swapping to the company's own model later is a config line.

**The call is recorded, and that is why it needs a session.** The ticket text this
sends is new content — the log holds it only because this call writes it — so the
request is built by `projection.build_side_request` (which appends a
`classifier/request` event carrying the prompt verbatim) and the call goes through
`log_guard.guarded_complete`. Without a session there is nothing to write it to,
and this provider **does not call the model at all**: an unrecorded model call is
the thing the invariant forbids, so it declines instead. A caller that wants a
classification passes a session; the fail-closed default is `needs_human`.
"""

from __future__ import annotations

import json
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.runtime.log_guard import guarded_complete
from forgepilot.runtime.projection import build_side_request
from forgepilot.seams.classifier import (
    SERVICE_NAME,
    ClassificationRequest,
    TicketUnderstanding,
)
from forgepilot.seams.session import EventKind

__all__ = ["LLMClassifierPlugin"]

#: The categories this classifier may return. Closed on purpose: a free-form
#: category cannot match a configured tool set, so it would fall back to the
#: default set — which is safe, but silently, and the model would have no way to
#: know it had done nothing.
CATEGORIES = ("bug", "question", "feature")

_SYSTEM = """You classify software tickets for an enterprise coding agent.

Reply with a single JSON object and nothing else:
{
  "category": one of "bug", "question", "feature",
  "symptom": what the reporter observes, in one sentence,
  "trigger": what precedes it, if stated,
  "actual": what happens,
  "expected": what should happen,
  "missing": [what you needed and the ticket does not say],
  "confidence": a number from 0 to 1
}

Rules:
- Use only what the ticket says. Do not infer a cause, and do not propose a fix.
- **`missing` is a hair trigger, so treat it as one.** A non-empty `missing`
  stops the task and asks a human to go and supply those things, so it must list
  only information whose absence makes the report **impossible to act on at all**
  — for example, a report that says only "it is broken" with no indication of
  what, or where, or how to see it.
  It is **not** a list of things you would like to know. The exact return type,
  the affected commit, the runtime version, how edge cases should behave, and
  anything a developer would simply read the code to find are all normal gaps
  that a competent engineer works around. Do not list them.
- The default for `missing` is `[]`. Answer `[]` unless the ticket genuinely
  cannot be started.
- Confidence is about how well you understood the ticket, not how likely you are
  to be right about the code. A well-described report about code you have not
  read is still high confidence.
- If the ticket is not about software at all, or you cannot tell what it is
  asking, answer with confidence 0 and list what you would need.
"""


class LLMClassifierPlugin(PluginService):
    """Serves ``classifier`` by asking the configured model."""

    provide = SERVICE_NAME
    inject = ["llm"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self.model = str(cfg.get("model", "gpt-4o-mini"))
        # Reasoning models spend output on thinking before answering, and a small
        # budget can be consumed entirely — returning an empty `content` that
        # looks like a refusal.
        self.max_tokens = int(cfg.get("max_tokens", 1024))

    def capabilities(self) -> frozenset[str]:
        return frozenset({"classify"})

    async def classify(
        self, request: ClassificationRequest, *, session: Any = None
    ) -> TicketUnderstanding:
        """Classify, if there is somewhere to record the call.

        **No session means no model call.** The prompt carries the ticket, which
        the log does not hold as history — so a call made without a session is a
        call nothing can rebuild, and *model-visible means logged* would be false
        for it. Declining is the fail-closed answer: the ticket goes to a human,
        which is the same thing that happens when the model is unreachable.
        """
        if session is None:
            return TicketUnderstanding(
                notes=(
                    "no session to record this classification in, so the model was "
                    "not asked; a human must decide"
                )
            )

        try:
            built = await build_side_request(
                session,
                kind=EventKind.CLASSIFIER_REQUEST,
                model=self.model,
                # Nothing from the ticket reaches this: a classifier copies ticket
                # text into its fields, and privileged text would speak with the
                # harness's voice.
                system=(_SYSTEM,),
                prompt=self._prompt(request),
                max_tokens=self.max_tokens,
            )
            response = await guarded_complete(session, self.ctx.llm, built)
        except Exception:  # noqa: BLE001 - an endpoint that breaks is not a guess
            return TicketUnderstanding(notes="the model could not be reached; a human must decide")

        return self._parse(response.message.content)

    # -- translation, kept separate from the call so it can be tested ---------
    def _prompt(self, request: ClassificationRequest) -> str:
        lines = [f"Title: {request.title}", "", request.description]
        if request.comments:
            lines.append("")
            lines.append("Comments:")
            lines.extend(f"- {body}" for body in request.comments)
        return "\n".join(lines)

    @staticmethod
    def _parse(content: str | None) -> TicketUnderstanding:
        """Anything unreadable declines. There is no path from here to a guess."""
        raw = (content or "").strip()
        if not raw:
            return TicketUnderstanding(notes="the model returned nothing")

        # A model may wrap its JSON in prose or a code fence despite being asked
        # not to. Recovering the object is safe; inventing one would not be.
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end < start:
            return TicketUnderstanding(notes="the model did not answer with JSON")

        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return TicketUnderstanding(notes="the model's JSON did not parse")

        if not isinstance(parsed, dict):
            return TicketUnderstanding(notes="the model's answer was not an object")

        category = str(parsed.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            # Not "the nearest one": an unrecognised category means the answer is
            # not the answer that was asked for.
            return TicketUnderstanding(notes=f"unusable category {category!r}")

        confidence = _confidence(parsed.get("confidence"))
        missing = parsed.get("missing") or ()
        if isinstance(missing, str):
            missing = (missing,)
        missing = tuple(str(m) for m in missing if str(m).strip())

        return TicketUnderstanding(
            category=category,
            symptom=str(parsed.get("symptom") or ""),
            trigger=str(parsed.get("trigger") or ""),
            actual=str(parsed.get("actual") or ""),
            expected=str(parsed.get("expected") or ""),
            missing=missing,
            confidence=confidence,
        )


def _confidence(value: Any) -> float:
    """Clamp to 0..1 rather than trust the scale.

    A model answering `95` means 95%. Read literally that is a confidence of
    ninety-five, and any threshold comparison against it is meaningless.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 1.0:
        number = number / 100.0 if number <= 100.0 else 1.0
    return min(1.0, max(0.0, number))
