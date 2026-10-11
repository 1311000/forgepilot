# Context compaction and the session log

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before touching `runtime/projection.py`, `runtime/context_*.py`, the compaction providers, or session storage**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## Compaction notes (P1-10)

The conversation lives in the log and grows every step, so a long run reached the
model's window and simply failed there. `providers/compaction/tiered.py` is what
happens instead. **`SurfaceOp.REPLACE` was the hook** — `derive_messages` had
implemented it since M3 and no runtime code had ever emitted one.

**Cheapest tier first, and the order is the design.** Every coding agent that has
published on this converges: replacing the *bodies* of old tool results costs no
model call and does most of the work, because tool output is ~84% of what grows.
Summarisation is the second tier, for when masking is not enough.

| tier | does | model call |
|---|---|---|
| `mask` | one `REPLACE` per old `tool/result`, carrying a placeholder with the tool name, size and both ends of the text | no |
| `summary` | whole turns replaced by a structured handoff | yes |

### ⚠️ The fold appended instead of replacing, and masking is what exposed it

`derive_messages` handled `REPLACE` as "remove the range, then append the
replacement". Measured on a two-pair conversation, masking only the first result:

```
assistant calls=['c1']      <- its result is not next
assistant calls=['c2']
tool    result_for='c2'
tool    result_for='c1'     <- three positions late
```

An assistant message whose `tool_calls` is not answered immediately after it is
rejected outright, and **the error arrives one turn later** with a traceback
pointing at the request rather than at whatever arranged the messages. The fold
now replaces the matching **block in place**, from the first match to the last.
Anything positioned between them belongs to the range: an element's position
follows its own original's position, so a replacement nested inside the range is
inside the range — which is why the block rule is correct and "only the matching
seqs" is not. `test_a_summary_over_a_masked_region_absorbs_the_placeholders` pins
that second-order case.

### Why masking cannot orphan a pair, structurally

It never touches an `assistant/message`. It replaces `tool/result` events, each
carrying **the same `callId`**, so the answer stays where the answer was. That is
a property of the shape rather than a rule someone has to remember.

Summarising *can* split a pair, so it works in **whole completed turns** —
`events.turn_spans` — and `snap_to_pairs` widens a region until no call inside is
answered outside. Both are belt and braces; the tests assert no orphans on the
**derived message list**, because that is the only place the two halves meet.

⚠️ **"Structurally impossible" was true of one pass, and the loop runs one pass per
step.** See "The second pass" below — the first ablation run killed 3 of 12
`tight-window` attempts with the exact 400 this section was written to prevent.

### ⚠️ The second pass: an append-only log has no "no longer there"

The ablation run's `tight-window` arm lost three of twelve attempts to

```
Messages with role 'tool' must be a response to a preceding message with 'tool_calls'
```

and the two in `out-of-scope` were reported as **`0 calls, 0 turns`** — an unset
counter, because the run raised before anything was summarised. Both are the same
defect, and it is the one this project keeps finding: **two call sites that read
the raw log and assumed it was the projection.**

A `REPLACE` does not mutate what it supersedes. The log is append-only, so the
original `tool/result` is still in it, still carrying `APPEND` — which is what
`select_mask_targets` filtered on, on the documented reasoning that *"a placeholder
carries `REPLACE`, so a second pass cannot mask a mask."* The placeholder does. The
**result it replaced** does not, and the result it replaced is what the walk sees.
So pass two re-selected every result pass one had masked.

Then the fold could not place the replacement: it matched a `REPLACE` by the **seq**
of the events it supersedes, and no visible event had those seqs any more — a prior
replacement was standing there, under its *own* seq. No hits, so it took the append
branch and put a `tool` message at the **end of the conversation**, answering a call
from the beginning of it.

Measured on the real log (`session-20261010T111533`), through the real
`derive_messages`:

```
seq=10  assistant  calls=['call_00_Qve4…', 'call_01_1wT1…']
...
seq=38  assistant  calls=['call_00_dC2i…', 'call_01_PE5W…']
seq=40  tool       result_for='call_00_dC2i…'
seq=42  tool       result_for='call_01_PE5W…'
seq=46  tool       result_for='call_00_Qve4…'   ← answers a call from six messages back
```

Three changes, and the first is the fix:

- **`events.superseded_seqs()`** — the missing question: *has something already
  taken this event's place?* Selection excludes those, so a second pass finds
  nothing to do — correct, and free. `saved` was also double-counted before this.
- **`_stands_within`** — a `REPLACE` whose own supersedes window overlaps the range
  *is* what stands there now, so the fold places in place instead of falling through.
  Not strictly needed once selection is right; needed because "this should not
  happen" is the reasoning that let it happen.
- **An unplaceable replacement carrying a `callId` is dropped, not appended.**
  Appending is right for a summary — a handoff over a region is new content — and
  wrong for anything that renders as a `tool` message. Dropping fails closed: the
  model sees the original body, *more* than the log promised, never less.

⚠️ **And the test that was supposed to catch it could not.** `assert_no_orphan_tool_calls`
walked *forward* from each assistant message, so it verified "every call is answered"
and was blind to a `tool` message answering nothing — which is the direction the
endpoint actually rejected. It is checked both ways now. Every compaction test also
called `compact` **once**; the loop calls it at the top of every step, and that gap
was the whole reason this shipped.

### What is never covered

- **The first `user/message`** — the ticket. Without it the run has no statement
  of what it is for.
- **The most recent `task/check`** — the repair loop's retry prompt tells the
  model to read the check output "above". Compacting it away leaves the prompt
  pointing at nothing.
- **The newest `keep_recent_turns` turns and `keep_recent_tool_results` results** —
  the model's foothold on where it is.
- **Anything at or below the last `compaction/summary`.** The barrier is the last
  *summary*, not the last `REPLACE`: a mask placeholder is not a summary, and
  refusing to summarise a region because it contains one would stop the tiers
  composing. A summary of a summary is what compounds errors.

⚠️ **Protecting a seq is not enough — the check is containment.** The brief is a
`user/message` *inside* its own turn, so a turn whose start and end are both
unprotected can still contain it. The first version checked only the boundaries
and summarised the ticket away while every check passed. Found by a test.

### Fail closed, and the breaker

A summary that **hit its output cap** is discarded, not kept: a truncated
checkpoint is worse than none, and every later step would reason from it. DSH
#3201 is that exact failure.

⚠️ **The check is on the usage we asked for, not on `stop_reason`.** **No provider
populates `stop_reason`** — it sits at its default, so a check on it would never
fire and the guard would look enforced and be nothing. `output_tokens >=
summary_max_tokens` is sound because we set the cap. `stop_reason` is consulted
too, so that when someone does populate it this sharpens on its own.

The breaker stops compaction after `failure_limit` consecutive failures, and it is
**counted from the log, not from memory** — a compactor is reconstructed per
session and per turn, so an in-memory counter resets on resume, and a resumed
session is exactly the one most likely to be near its window.

⚠️ **Only failures that cost a model call count.** The breaker exists to stop
*spending*, so a pass that spent nothing is not evidence of thrash — and
`no-eligible-region` is both the code that spends nothing and the code whose
condition **resolves itself**: a region below the summary floor grows past it as
the conversation does, and a fully masked conversation keeps producing new results
to mask. Counting it latched the breaker **permanently**, because it cannot
unlatch: it blocks the very pass that would record a success and reset the streak.
Every step of the real `tight-window` run logged exactly this, and the first
attempt to replay the sequence gives

```
['no-eligible-region', 'no-eligible-region', 'circuit-open']
```

`COSTLY_FAILURES` is `{model-error, truncated, empty}` — the three that mean a
summariser call was made and came back useless.

### Things worth keeping

- **The pass runs before `build_request`, and that is mandatory rather than
  tidy.** `build_request` writes a log-only `request/header` **first**, so
  compacting afterwards records a header describing a request that was never sent;
  and `guarded_stream` flushes then requires every `source_event_seq` to be
  durable, so an event appended after the request was derived would trip
  `ModelVisibleNotLogged`. The estimate is therefore computed twice — once to
  decide, once as the unchanged loud backstop.
- **The summariser call goes through `projection.build_compaction_request`.** That
  module claims to be the only constructor of a `ChatRequest`, and the claim is
  already false twice (the classifier and the review narrative each build their
  own and neither is logged). Compaction is not a third: it goes through
  `guarded_complete`, so the call is flushed, guarded, and reconstructable from
  the log. The honest description of the invariant is *"every conversation message
  comes from the projection"*, not *"nobody else ever builds a request"*.
- **Failures are recorded, never raised.** A pass that cannot act is not an error
  in the run — it means the run is on its own from here. `compaction/failed`
  carries one of five codes, and the breaker counts them.
- **`compactor=None` is byte-for-byte the old behaviour.** It is the default
  argument everywhere, so a profile with no `compaction` row is unchanged, and
  every test that predates this exercises that path.
- **The provider takes `llm` as an argument** rather than injecting it. It is the
  same model the loop is using, and a provider that held its own would summarise
  with a different one than the conversation is happening in.

**Known gap: `estimate_tokens` is `len // 4`**, so the trigger and the "freed"
accounting are approximate. It errs toward refusing early, which stays safe here
for the same reason: compacting a step too early costs a little, a step too late
has already failed. A real tokenizer is a dependency and a per-model decision, and
is its own task.


## Session log notes (read before touching storage or the projection)

The design was informed by reading how DeepSeek Harness and Claude Code actually implement theirs. What was adopted, and what was deliberately not:

- **Adopted from DSH**: contiguous `seq`, `surface` ops (surface vs log-only), the `session/end-seed` boundary that delimits a resumed/forked prefix, flushing at turn boundaries, closing an interrupted turn rather than dropping it, and compaction-instead-of-mutation.
- **Adopted from Claude Code**: spilling oversized payloads to a sidecar instead of inlining them, per-session single files, and *not* compressing — when a run goes wrong, being able to `grep`/`jq` the timeline is worth more than the disk.
- **Not adopted**: Claude Code's `parentUuid` linked list. It unlocks rewind and arbitrary-point forking, but costs a second ordering to keep consistent with `seq`, and a fork boundary already achieves the useful cases. `event_id` is already there, so adding it later is purely additive.

Rules that are easy to get wrong:

- **`seq` is assigned by `Session`, validated by the store.** This reverses the M2 contract deliberately: a buffered session must know a seq *before* flushing, because a model-visible message records `source_event_seq`. The store rejects a non-contiguous batch rather than renumbering it — silently correcting a caller's seq would hide the bug that produced it.
- **`SurfaceOp` defaults to `LOG_ONLY`.** Forgetting to mark an event surface makes it invisible to the model — loud and obvious. The opposite default would silently send the model something nobody meant to send.
- **Spilling is lossless.** The blob is read back and the digest verified, so replay reproduces exactly what was written. The tempting shortcut — keeping a shortened copy — breaks the invariant the first time anyone replays a session, which is precisely when it matters.
- **A torn tail is discarded; an open turn is closed.** Different cases: the first was never a record, the second is history that really happened. Corruption *away* from the tail raises `CorruptLog` rather than being repaired — a log with a hole in the middle is not a shorter log.
- **`guarded_stream`/`guarded_complete` flush before dispatch.** "Remember to flush first" is a rule that gets forgotten in a new code path, so the guard does it rather than documenting it.
- **Adding a surface event kind means adding a renderer.** `projection.unsupported_event_kinds` raises rather than skipping, because an unrendered surface event means the model saw less than the log says it saw.
- ⚠️ **A `REPLACE` takes the place of the range it supersedes — it is not appended.** The first version removed the superseded events and appended the replacement to the end of the visible list. For a whole-span summary that is nearly harmless, which is why it survived; for masking a single tool result it is fatal, and it was measured. See "Compaction notes".
- **`seams/` must not depend on `kernel/`, even transitively.** That is why `ForgePilotError` lives at `forgepilot/errors.py` and not inside `kernel/`: importing `forgepilot.kernel.errors` would execute `kernel/__init__`, which imports `cordis_port`.

