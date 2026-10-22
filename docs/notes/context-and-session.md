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
- **Every model call is recorded, and the projection is the only place a request
  is built — since M16 that is enforced rather than asserted.** This bullet used
  to concede that the module's claim was already false twice: the classifier and
  the review narrative each built their own request and neither was logged. Both
  now go through `projection.build_side_request`, which appends the prompt as a
  log-only `classifier/request` / `review/request` event and hands back a request
  whose single message cites that event — so the call is reconstructable from the
  log, *and* what the model is shown is what the log holds, because the request is
  rebuilt from the event (the redaction gate therefore sits between the two).
  Compaction was already safe here: `build_compaction_request` plus
  `guarded_complete`. Two contract tests now parse the tree: a `ChatRequest` may be
  constructed only in `runtime/projection.py`, and a model may be reached only
  through `runtime/log_guard.py` — both with **no exemption list**, since an
  exemption list is where the next offender hides and it is edited by whoever adds
  the offender. The honest description of the invariant stays "every conversation
  message comes from the projection"; what changed is that the narrower claim is
  now the true one.
- **Failures are recorded, never raised.** A pass that cannot act is not an error
  in the run — it means the run is on its own from here. `compaction/failed`
  carries one of five codes, and the breaker counts them.
- **`compactor=None` is byte-for-byte the old behaviour.** It is the default
  argument everywhere, so a profile with no `compaction` row is unchanged, and
  every test that predates this exercises that path.
- **The provider takes `llm` as an argument** rather than injecting it. It is the
  same model the loop is using, and a provider that held its own would summarise
  with a different one than the conversation is happening in.

**`estimate_tokens` is a calibration, not a tokenizer** — four ASCII characters
per token, and a non-ASCII character counted as a whole one. It used to be
`len // 4` for everything, which over-counts English (the safe direction: the
trigger fires early) and under-counted CJK by roughly 4×, so a 1000-character
Chinese prompt was estimated at 250 tokens — the *unsafe* direction, in a system
whose tickets, comments and briefs are routinely CJK. The repair keeps the single
floor at the end, so every English-only number is byte-identical, and moves only
non-ASCII text upward; the measured consequence is that no offline number moves
at all (`--scripted`, both arms, 20 rows: `compactions=0`, `shadowed=0`). A real
tokenizer is still a dependency and a per-model decision, and is still its own
task.


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
- **`summaries()` is newest first, and it is the only listing the store offers.** Ordering is stated on the Protocol because the two implementations disagree by construction — the memory store's insertion order and the durable store's directory are not the same order, and "whatever the implementation returns" would push a sort into every caller. The durable store pays a header read per session for it (no time index at the skeleton's scale); the memory store reverses. The first caller was `GET /tasks`, which needs "what did this principal run" after a restart — before that the method had two implementations and no reader at all, and the route's own comment said why.


## Redaction: the gate in front of the log

Every event that enters the log passes `runtime/redaction.py` first. The gate lives in
`Session.append` — the one function that may write an event — and nowhere else.

**Why there, and not somewhere more obvious.** Decided against two alternatives that
each look tidier:

- **Not in the projection (read time).** Model-visible ⇒ logged, and the log is what
  the projection reads, so gating the write covers *both* the log and the model with
  one code path. Gating the read instead would leave the plaintext on disk — the
  failure the whole exercise is about — and would make every future reader of the
  log (the distiller, an export, a replay) responsible for remembering to redact.
- **Not in the JSONL store.** `providers/session_store/jsonl.py` spills any payload
  over `DEFAULT_INLINE_LIMIT` (64 KiB) into `blobs/<tenant>/<event>.json` and *clears
  the event's `data`*. A store-level gate would therefore miss every large tool
  result — exactly the payload most likely to contain a credential or an intranet
  URL. There is a test for that, and it is one of the two that only pass because the
  blob is written from an already-redacted payload.

**Rules are ordered, and the order is load-bearing.** `private-key, token, jwt,
auth-header, auth-scheme, assigned-secret, url-userinfo, internal-url, high-entropy`:

- The PEM rule runs first; a private key body must be consumed whole rather than
  shredded into token-looking fragments.
- The two auth rules run **before** `assigned-secret`. With them after, the text
  `authorization: Bearer <token>` matched the assignment rule with `Bearer` as the
  "value" — so the placeholder replaced the *word* and left the token in the log.
  The payload-walk test caught it; the ordering comment records it because the next
  person to add a rule will reorder the tuple without thinking.
- `high-entropy` runs last so it only sees what no named rule claimed.

**Two rules exist because of false positives, not false negatives.** `_ASSIGNMENT`
requires `(?![A-Za-z0-9])` after the credential word, or `max_tokens=200000` matches
(it contains `token`) and a budget number becomes a placeholder. `_SENSITIVE_KEY`, the
dict-key test, matches a whole `_`/`-`-delimited segment, so `max_tokens` is not a
token but `api_key` is. A detector that fires on ordinary identifiers is switched off
within a week, and then everything leaks — the same lesson `..` and the eval arms were
chosen for.

**Failure degrades; it never refuses the write.** There is no `RedactionFailed`, and
that is deliberate: a gate that can stop a run becomes a gate that gets bypassed. A
rule that raises turns *that string* into `«redaction-failed:bytes=N,fp=…»` and emits
`redaction/failed`; the event still lands, the next event still lands, and the run
continues. The stub's digest is a **salted HMAC, not a bare sha256** — a bare digest
lets anyone confirm a guessed payload against the log, which is a secret-disclosure
oracle built out of the safety feature.

**At `SECRET`, the payload is withheld, not minimised.** A match classified
`Sensitivity.SECRET` collapses the *whole* payload to `«redacted:secret-event:bytes=N»`
rather than leaving a typed placeholder inside it. The reason is that a pasted
credential is evidence about the rest of the payload: the words around a token — a
hostname, a project name, an internal path — are exactly what a minimised redaction
would have preserved, and the doctrine this was built from says the highest level
degrades to metadata. Three properties are deliberate:

- **Keys and non-string fields survive.** Everything downstream looks payloads up by
  name (`data["text"]`, `data["callId"]`), so a payload that lost its shape would break
  the conversation instead of protecting it — and would break it one turn later, when
  the projection next runs.
- **The stub carries the size; the fingerprint is in the audit event.**
  `redaction/applied` records `withheld: true`, so a reader can tell "this turn happened
  and its text is here" from "this turn happened and everything it carried was
  withheld". Without that flag the two look identical in the log.
- **The cost is real and is not hidden: a user message that contains a pasted key
  reaches the model as a stub**, so the agent cannot act on it. That is the intended
  direction of the trade — the alternative keeps text a `SECRET` classification says
  not to keep — and the escape hatch is the credential channel, not an exemption here.

One consequence is worth recording because it nearly cost a test its meaning: the blob
test (`test_a_payload_that_spills_to_a_blob_leaves_no_secret_in_the_blob`) had to switch
from a token to an `INTERNAL`-classified URL. A token-bearing payload now collapses, so it
never grows past the inline limit and never spills — the test would have been *skipped*
on the jsonl store and silently stopped covering the one path it exists for.

**Fingerprints are salted HMAC-SHA256, truncated to 16 hex.** Salt comes from
`FORGEPILOT_REDACTION_SALT`, else it is random per process — so without a configured
salt, fingerprints are comparable only *within* one process. That is the honest
behaviour; a stable cross-run digest would be the oracle above.

**There is no kill switch.** `Redactor()` compiles every rule; only `high-entropy` has
an `enabled` flag, and it is off by default because its false-positive rate is
unmeasured. A configuration key that turns the gate off is a key that is off in
production, so it does not exist.

Deployment-specific knowledge enters through the environment — `FORGEPILOT_INTERNAL_HOSTS`
(a comma-separated list of the company's own domains) and `FORGEPILOT_REDACTION_HIGH_ENTROPY`.
Not through a profile row, and not as a `Session` parameter: the only `Session.open`
call sites are `runtime/assembly.py:110` and `:121`, `assemble_session` has no profile
in scope, and a list threaded through every entry point would be forgotten by exactly
the caller that writes the intranet URL. The environment is the same channel the salt
and every other deployment secret already arrives through (`config/env_file.py`).

**Measured, not declared.** `tests/security/test_redaction_never_writes_a_secret.py`
runs each case against both stores. With the gate line in `append` replaced by
`redacted, report = dict(data or {}), RedactionReport()`, **11 of its 14 cases fail** —
the three that survive are the one asserting an ordinary event passes through
untouched, and the in-memory store's missing-blob skip. Restored, the full gate is
`1092 passed, 220 skipped`, 88.04%, `All gates passed.`

⚠️ **What the gate does not do.** No field-level encryption and no vault, so the
*placeholder* is the only thing protecting the shape of the original; the original is
not recoverable for a replay, and `docs/notes/memory.md`'s Phase A loses nothing by
that because a redacted log is what the distiller is supposed to read. No NER or
schema-aware detection, so a secret with no recognisable shape survives. And the read
side — a memory record refusing to be recalled above a reader's `Sensitivity` ceiling —
is Phase E work; `Sensitivity` is already in `seams/security.py` for it.


## The credential channel: `${NAME}` instead of a secret

The redaction gate keeps secrets *out*, and a key the user pastes into a message reaches
the model as a stub — which is the intended direction of that trade, but it means the
agent cannot use it. The credential channel is the other half: the model names a secret
it never sees, and the value is substituted after the human has approved the call.

```
model writes:  git push https://x-access-token:${github_token}@github.com/acme/repo
arm phase:     claims the name — declared? non-empty? — logs `credential/claimed`
execute:       provider resolves it, the sandbox argv carries the value
log / model:   the placeholder, everywhere, always
```

**The seam** is `seams/credentials.py`: `CredentialChannel.refs()` / `.resolve(name)`,
plus `placeholder(name)` and `placeholders(text)`. The shipped provider
(`providers/credentials/env.py`) maps a declared name to an environment variable and
reads `os.environ` **at resolve time**, so a `.env.local` loaded after boot still works;
rotating the variable between two resolves changes the answer, and there is a contract
test for exactly that. It is deliberately not an environment dump: an undeclared name
raises `UnknownCredential` ("no secret is declared under the name …"), as does a declared
name whose variable is empty.

**It is claimed in `arm`, before capabilities are minted.** `arm` is the only phase
allowed to touch host state, so it is the only place a name may be resolved at all — and
the claim runs *before* `mint_capabilities` on purpose: a command needing both a rewrite
(unknown credential) and a stronger sandbox would otherwise be told about the sandbox
first, and the model would "fix" the wrong thing. The claim appends `credential/claimed`
carrying the **names** and nothing else — a name is not a secret, and an audit that hides
which credential a command asked for is useless.

**A secret-shaped literal is refused, not redacted.** `literal_secret()` runs the
redaction rules over the command `arm` is about to authorise; any *named* rule match
(`high-entropy` excluded, even when a deployment enables it — "looks random" is not a
defensible thing to refuse) raises `LiteralSecretRefused`. Its message names the rule
kind, says secrets come from the credential channel, shows the form to write
(`${github_token}`), and deliberately does not echo the literal. Redacting instead would
leave the model holding a secret it typed, having silently converted it — the channel is
worth nothing if a model can bypass it by being literal. `LiteralSecretRefused` subclasses
`ArmRefused`, so the phase's existing failure handling covers it and a refusal is a
refusal. When no channel is configured at all, `${NAME}` is refused too: running the
command with the placeholder left in it fails somewhere else and looks like something
else.

**Three places a value surfaced that no rule could ever catch.** All three are now
scrubbed, and the first two were found by tests rather than by review:

1. **The not-executed diagnostics.** The refusal-ish path builds its message from
   `not_executed_reason(result)`, which prints `argv: list(result.argv)` — the
   *substituted* argv. The value reached the tool result, and from there the durable
   log, through the diagnostics rather than through the command.
2. **The command's own output.** `echo`, `set -x`, a traceback: whatever the sandbox
   prints is part of the tool result. This is precisely where a rule set cannot help,
   because the whole premise of the channel is that a secret need not *look* like one;
   the fix is value-aware (`scrub_values(body, secrets)`), not pattern-aware.
3. **The recorded argv in the outcome meta.** Now scrubbed element by element, with
   `"secrets": sorted(names)` beside it — names, not values.

**Absence is a refusal, not a bypass.** `ToolPipeline.credentials` defaults to `None` and
`assemble_session` reads it off the root, so a profile with no `credentials` row refuses
every `${NAME}` in `arm`. The default profile ships the row with `entries: {}`, which is
why `--boot` now reports **19 rows ACTIVE**.

**Measured.** `tests/security/test_credentials_are_never_literals.py` is the D2-4
acceptance row: a literal `ghp_…` is refused with `phase == "arm"` and the recording
sandbox sees no call at all; `${demo_token}` reaches the sandbox with the real value in
its argv while the outcome meta, the tool result and the whole log hold the placeholder;
an unclaimed name is refused. `tests/contract/test_seam_contracts.py` covers the seam.
Full gate at this point: `1125 passed, 225 skipped`, 8104 statements, 88.25%,
`All gates passed.`

## A session log the database writes (P16 ①, 2026-10-22)

The JSONL store assigns nothing: `Session` picks the number before the write, and the
store refuses a batch that does not continue the log. That is correct for one writer
and it is what a single-replica deployment runs. It cannot be the only design, because
two replicas that each compute "last seq + 1" from a value they read a moment ago both
write the same number, and a log with two `seq = 7` is not repairable.

`providers/session_store/sqlite.py` is the store where the database decides:

- `append` assigns sequence numbers inside `BEGIN IMMEDIATE`. A caller that passes a
  number is validated (a gap is refused, never renumbered); a caller that passes `0`
  is given the next one.
- `recover_tail` says there is nothing to trim — SQLite commits per transaction, so a
  torn tail cannot exist. It does not pretend to inspect anything.
- `fork` copies rows inside one transaction, so a child can never see half a parent.
- WAL, because a replica serving `GET /sessions/{id}` must not wait behind another
  replica's append.

It runs the same contract suite as the other two stores
(`tests/contract/test_session_store_contract.py` is parameterised over `memory`,
`jsonl` and `sqlite`), and `tests/providers/test_the_store_assigns_the_sequence.py`
covers what that suite cannot express: two independent connections appending to one
session interleaved produce a contiguous log of 20 events, and a caller-supplied gap is
refused without spending a number.

What it does not do: blobs. The JSONL store spills large payloads beside the log because
a megabyte-long line breaks every tool that greps the file; a row has no such problem,
so payloads stay inline. Load it with `--patch src/forgepilot/bundles/session_sqlite/patch.yaml`
when running `--multi-replica`; it replaces the row rather than adding a second store,
because two stores means two histories.
