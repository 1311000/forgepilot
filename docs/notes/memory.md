# Memory permissions and distillation

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before touching `memory` or starting distillation**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## Memory notes (read before touching `memory` or building distillation)

**Both sides now have a caller.** M11b Phase C runs `memory.write`/`admit_provisional`
from `runtime/distillation.py` and emits `memory/write` for the first time; Phase E
reads them back — `runtime/recall.py` recalls once per turn and emits `memory/recalled`
carrying the exact text the model was shown. Until Phase E, **nothing in `src/` called
`recall(`** (Phases A–D are the commits `a47765e`, `da79c4b`, `ed6f360`, `482dbd3`), so
memories were recorded and never read back; that is worth continuing to say plainly,
because "the memory feature" reads as done when you see the tests pass. Phase D adds a
*durable* provider of that store, which changes where the records live and nothing about
who reads them.

**M11's stated acceptance row was already green while the property it named was
false.** `write(MODEL_ASSERTED)` raising was real; "cross-repo recall returns
empty" held only for records whose `provenance.repo` happened to be set *and*
agreed with the caller. Two leaks, both demonstrated before being fixed:

- the filter was written `if repo is not None and not allows_repo(repo)`, so a
  record with no repo on its provenance **skipped the permission check entirely**;
- `scope_ref` and `provenance.repo` were treated as interchangeable, so a record
  scoped to one repository but *learned in* another was authorised by the wrong
  field. That mismatch is the expected shape of a fact learned in repo A about
  repo B — not an exotic one.

The existing contract test never caught either: its "other repository" record was
**quarantined** (so provenance filtered it, not permission), and its recallable
record had `repo=` set with a matching caller. The path "recallable record +
unauthorised caller" had never once been executed.

Rules now enforced, and where:

- **The audience is `scope_ref`, and it is checked once, on the query.** Every
  record that can match shares the query's scope, so deciding it up front is both
  simpler and impossible to forget per-record. `provenance.repo` is a *second,
  independent* limit — where the fact was learned — and can only ever narrow.
- **`write` refuses three things**, all as `UnprovenMemoryWrite`: a non-recallable
  `kind`; an empty `evidence_refs`; an unattributable `scope_ref`. Each is refused
  at write rather than filtered at recall because a write happens once and a
  recall happens many times.
- **`evidence_refs` being non-empty is now a mechanism, not a comment.** The
  docstring always said "a record with no evidence is an assertion, not a
  memory"; only `kind` was checked, so anything could be admitted as
  `TEST_VERIFIED` while citing nothing. Same argument as M5's unsigned
  `ExecutionConstraints`: omitting the evidence must not be the cheap way past
  the evidence requirement.
- **An undefined audience raises; an unreachable one returns `()`.** Only `REPO`
  scope has a defined audience model — the plan's assumption that a memory scope
  "mirrors the repository scope" collapses for `TEAM`/`PROJECT`/`ORG`, because a
  team is not a set of repositories. `recall` raises `UnattributableScope` for
  those, because an empty result would read as "no memories here" when the truth
  is "nobody has said who may read these". Both are fail-closed; they are not the
  same answer and must not look alike.

**Known gaps, deliberately left:**

- ~~**There is no tenant dimension.**~~ **Closed by M11b Phase B**, and not the way
  this paragraph predicted. The suggestion was for `write` to take
  `ExecutionConstraints`; it does not. The tenant rides on
  `MemoryCandidate.tenant_id` and is **required** — `UnattributableTenant` refuses a
  candidate that names none. The reason: `ExecutionConstraints` is the *caller's*
  authority and is consumed at recall, where the comparison happens; a record's
  tenant is a property of the record, and the distiller that builds the candidate
  already has the session header it came from. Requiring it rather than defaulting
  it is the whole check: a record with no tenant is not under-classified, it is a
  record that passes *every* comparison.
- **`revoke()` has no callers, and `MemoryRecord.supersedes` is never set.**
  Forget/Rollback is the least-exercised phase of every memory lifecycle. Phase B
  moved `revoke` off a side-set of ids onto the status machine (with
  `superseded_by`/`supersedes` waiting for Phase F), so at least the state it
  produces is now the same one recall filters on.
- **Distillation's gate is answered, and the answer is D1.** The design doc's R7
  ("session logs will become a training-data source; the privacy question must be
  answered before M11") is settled by the plan's **D1**, which the user accepted:
  memory is produced only from this repository's and this tenant's sessions, always
  `MemoryScope.REPO`; the raw log is never used to train anything and never leaves
  the tenant; model text reaches only the proposal layer unless an *event* endorses
  it; and **reading content is not evidence that content is true**. What that does
  not settle — and the plan says so — is that the model behind the `llm` seam may be
  a commercial endpoint, so "what leaves the machine" is a deployment choice, not a
  property of this code. Redaction decides what may be *written down*; D1 decides
  who may *read* it back.

## What M11b has actually built so far

**Phase A — redaction and the credential channel.** Evidence lives in
`docs/notes/context-and-session.md` (`## Redaction: the gate in front of the log`,
`## The credential channel: ${NAME} instead of a secret`), because that is the file
for the write path it hooks. In one line: `Session.append` is the only gate (so
model and log are covered together), a secret-shaped literal in a command is
*refused* rather than redacted, and the three places a substituted value used to
leak — the not-executed diagnostics, the command's own output, the recorded argv —
are scrubbed by value rather than by pattern.

**Phase B — the record.** Every field below has a reader, and the reader is named
because a field with none is this repository's recurring defect:

| Field | The decision it changes |
|---|---|
| `type` (8 kinds) | where it may be recalled — `MEMORY_TYPE_MODES`; a `TASK_SUMMARY` is not recited back during a chat |
| `confidence` | whether it is stated as fact at all (`MemoryRecord.is_verified`) |
| `status` | whether it is served, and as what — `PROPOSED` is recallable *as a proposal* |
| `subject` | conflict detection (Phase F's `conflict_key`), and half of the de-duplication key |
| `content_hash` | one record per claim, however often distillation runs |
| `tenant_id` | the outermost audience, checked before the repository |
| `recall_count` / `last_recalled_at` | "was this ever used" — nothing else can answer it |
| `supersedes` / `superseded_by` | the supersession chain Phase F builds: which claim replaced this one |
| `expires_at` | a plan stops being current (`MEMORY_TYPE_EXPIRY_DAYS`), a convention does not |

Two decisions inside that are worth not re-litigating:

- **Belief is computed, never claimed.** `confidence_for(kind, evidence)` reads the
  provenance kind the *harness* assigned and counts distinct `EventRef`s; two
  independent events raise one tier, belief saturates at `HIGH`, and the model's own
  number survives only as `candidate.model_confidence_hint`. A caller cannot pass a
  confidence to `write`, and `admit_provisional` takes no arguments that could set
  one.
- **`evidence_refs` is typed (`EventRef(session_id, seq)`), not a string.** A bare
  `"seq:12"` cannot be looked up, so the "verify the citation" half of the evidence
  rule was unimplementable. Nothing checks existence yet — that is Phase C, where a
  store is in scope — but the check is now expressible.

**Phase B measured:** `tools/check.py` → `1142 passed, 225 skipped in 65.38s`,
8261 statements, 955 missed, **88.44%**, `All gates passed.` (Phase A was
`1125 passed, 225 skipped`, 88.25%.) The model's own tests are
`tests/contract/test_memory_model.py` (17 cases); the M11a contract tests in
`tests/contract/test_seam_contracts.py` were updated to the typed refs and the
tenant requirement rather than worked around.

**Phase C — distillation.** `runtime/distillation.py::distill()` reads one stretch of
session log and proposes memories; `seams/distillation.py::witness_for()` decides what
may be believed. The split is the point: the model's answer is a list of
`{type, subject, text, evidence_seqs, model_confidence_hint}` and **nothing else** —
`status`, `confidence`, `provenance`, `tenant_id` and `id` are not fields it can set,
and `parse_candidates` *refuses* an answer that carries one rather than quietly
ignoring it.

The load-bearing rule, now executable: **the harness reads provenance off the event
that evidences it.** `witness_for(log, cited)` resolves a candidate's citations in
order — a cited `user_message` is `HUMAN_VERIFIED`, a cited `task/check` with `passed`
and not `couldNotRun` is `TEST_VERIFIED`, a cited non-error `tool/result` whose
`callId` names an **effect** tool (`run`, `write_file`, `edit_file`) is
`TOOL_OBSERVED`, and nothing else is `MODEL_ASSERTED`. A cited `read_file` /
`code_search` result confers **no** trust, and `READ_ONLY_TOOL_NAMES` is a declared
list rather than a fall-through so a new tool is *unclassified* rather than silently
trusted. Reading a malicious file therefore cannot launder it into memory — and the
read is quoted in the reason the candidate was not believed, because "never trusted"
and "trusted and then found wanting" are different facts about a model's answer, and
only the second is a conversation worth continuing.

Three mechanisms keep the run honest, in the order they bite:

- **The window.** Only closed turns after the watermark are distilled — `window_for`
  stops at the last `turn/end`, so an open turn stays for the next pass, and the
  prompt's evidence digest (`evidence_digest`, capped at `EVIDENCE_DIGEST_CHARS`)
  lists exactly the seqs that may be cited, as a *second system section* rather than
  fabricated conversation.
- **The citation check.** Every `evidence_seqs` entry must be a model-visible event of
  that window. A candidate citing anything else is dropped, and the drop is recorded
  with its subject and the offending seqs — **never its text**.
- **The watermark**, one per session, moved to the last closed turn **only when a run
  finishes**. It is what makes distillation idempotent; the store's content hash is a
  backstop, not the mechanism.

**Phase C measured.** `tools/check.py` → `1189 passed, 230 skipped in 66.98s`,
8752 statements, 1026 missed, **88.28%**, `All gates passed.` (Phase B was
`1142 passed, 225 skipped`, 8261 statements, 955 missed, 88.44% — the percentage dips
because the CLI surface and the failure paths are only partly reachable from a test.)
The two counterfactuals
the plan names, measured by patching `src/` and re-running
`tests/memory/test_distillation.py` (24 cases): **citation check removed → `2 failed,
22 passed`** — an invented `9999` lands `HUMAN_VERIFIED`/`ACTIVE`/`HIGH`, because
`witness_for` ignores a seq that is not in the log — and **watermark never persisted →
`3 failed, 21 passed`**. Supporting files: `tests/providers/test_watermark_store.py`
(a torn last line is skipped so the watermark stays *lower* than reality, a watermark
never moves backwards, loading a profile writes nothing) and
`tests/cli/test_distill_cli.py` (refusals, and exit 4 for a session that is gone).

**A failed run writes nothing and does not advance the watermark** — the `budget` path
exists so a retry with a larger cap sees the same stretch again. `distillation/start`
is flushed on its own (the unmatched-start pattern compaction already uses), the raw
model answer is recorded in `distillation/summary` *before* it is parsed so a schema
failure is auditable, and `memory/write` — declared since M3, emitted by nothing until
now — carries the memory id, subject, witness kind, status (verified vs provisional,
as a *pair of disjoint counts* rather than one number) and the reason. `distillation/
failed` carries codes `llm`, `schema`, `budget`, `no-scope`, `no-tenant`, `no-model`;
the last three are pre-flight refusals that emit no events at all, because a run that
never started has nothing to audit.

**Known Phase C gaps, stated rather than hidden:** there is **no scheduler** — the
plan's "periodic + session end" is an operator's `forgepilot --distill <session>
--repo host/namespace/name`, plus `--since`, `--dry-run` and `--distill-token-cap`,
and there is no `--all` sweep; a run that produces only proposals reports
`written=0, provisional=10`, and the CLI prints both because a single number would
hide which happened; and a *dropped* candidate still lets the watermark advance, since
retrying a stretch whose citations were bad would fail identically forever — that is a
stall, not safety.

**Phase D — the durable store, and the rule that a second provider may not re-decide.**
`providers/memory/sqlite.py` is the implementation the design doc left room for: one
file, a `memories` table plus two indexes — `memories_content(content_hash)` for
de-duplication and `memories_audience(tenant_id, scope, scope_ref, type, status,
subject)` for recall — with the whole `MemoryRecord` stored as JSON in `record_json`
and the columns used **only** for indexing and filtering. That split is deliberate: a
column-per-field schema silently drops a field someone adds to the record later, which
is the "looks persisted, isn't" version of the repeated defect.

Adding the second store answered a question the first one had been hiding: **where the
rules live**. Both stores decide "may this be believed" (the admission gates) and "may
this caller see it" (the recall filter), and two copies of that decision is the defect
shape this repository keeps meeting — with the twist that two copies *agree* until one
of them is edited. So the decisions moved into `seams/memory.py` as pure functions over
the value objects, and both stores became storage:

- `require_recallable_provenance` / `require_provisional_provenance` / `require_tenant`
  — the write gates, each raising the seam's own error (`UnprovenMemoryWrite`,
  `UnattributableTenant`). The refusal messages are the ones the providers already had,
  because they are the ones a test asserts on.
- `new_recallable_record` / `new_provisional_record` / `new_quarantined_record` /
  `promote_proposal` / `confirm_record` / `revoke_record` / `expire_record` /
  `count_recall` — the field assignments that must not drift between stores.
- `may_recall(query, constraints)` and `recall_verdict(record, query, constraints) ->
  RecallVerdict` (`HIT`/`SKIP`/`EXPIRED`) — the audience check and the per-record
  filter, in that order, with `now` a parameter rather than a clock read.

The single-sourcing found one behaviour change on the way: `admit_provisional` used to
drop `candidate.expires_at`, so a proposal's deadline was silently lost. `PROPOSED` is
a recallable status, so the deadline is what retires a guess nobody endorsed —
`tests/providers/test_sqlite_memory_store.py::test_an_expired_proposal_is_retired_rather_than_left_standing`
is the regression test, and it is only observable because expiry is checked *before*
the provenance kind.

**The contract suite now asks both stores the same questions.**
`tests/contract/conftest.py` gained `MEMORY_PROVIDERS` and a parametrised
`memory_store` fixture (`ids=["in-memory", "sqlite"]`); `test_memory_model.py` keeps
its 15 bodies and gains a one-line local `container` override, so **which provider am
I talking to cannot change the answer to "may this be believed"** — 30 cases, measured.
This is the pattern for the next provider of any seam: a second implementation passes
the first one's tests, not its own.

What only a file can be asked is in `tests/providers/test_sqlite_memory_store.py` (9
tests): a memory survives the process that wrote it; **loading the store does not
create the file** (the same rule `providers/distillation/watermark.py:48-50` keeps, so
`--boot` stays side-effect free); the same sentence in two tenants is two memories and
neither leaks; a memory scoped to another repository is served only to a caller who may
read *that* repository; a revocation survives a restart; the same claim written in two
processes is one row; and the milestone's actual claim,
`test_a_second_process_recalls_what_the_first_process_distilled` — process 1 boots a
root with the SQLite store and a `ReplayLLMPlugin`, distils one session, prints
`written=1` and **exits**; the test process then opens the same file and recalls the
memory. Without that test, "memory across sessions" is a claim made inside one
interpreter, where an in-process dict passes every other case in the file.

Two decisions inside the store that are easy to get wrong a second time:

- **Ids are derived from the content hash** (`mem-<hash[:16]>`), not counted. A
  per-process counter makes two processes distilling the same session both hand out
  `mem-1`, and the second insert loses to the primary key — a lost memory whose
  symptom is silence.
- **A connection is opened per operation and held in a local**, never cached on the
  instance. Half of that is the Cordis service proxy (see
  `docs/notes/seams-and-providers.md`: a *callable* attribute read back through
  `ctx.memory` comes out as a shadow method, and `sqlite3.Connection` is callable);
  half is that a per-operation connection cannot hold a lock another process wants.

**Phase D gaps, stated:** the schema has **no version or migration path** — the file is
new, so this is a decision deferred, not solved, and the first change to `MemoryRecord`
that the JSON codec cannot default is where it has to be faced; `revoke` is still the
only forgetting mechanism with a caller (Phase F adds `purge`), and nothing prunes old
rows; the file is a single SQLite database, so it belongs on a local filesystem, not on
a network share (warned about in the bundle's comment). The default profile is
unchanged: `bundles/memory_sqlite/patch.yaml` is a **patch-only bundle**, applied with
`--patch`, so `--boot` still resolves 20 rows and the in-memory store is still what a
deployment gets unless it asks otherwise.

**Phase E — recall, and the rule that every recalled line is labelled.** `runtime/recall.py`
is the whole feature; it is called once per turn from `runtime/loop/turn.py`, immediately
after `session.user_message(...)` and before the step loop, because "which repository is
this turn about" is settled by what the user just asked. Four things it decides, each one
a place the obvious implementation is wrong:

- **Which repository, and the refusal when there is not exactly one.** `recaller_for()`
  returns `None` unless `constraints.allowed_repos` holds exactly one repo. Zero repos
  is the common case and not an error: `ProjectPolicy.open_project()` leaves `repos`
  empty, which is exactly what `forgepilot --chat` builds, so **CLI chat has no
  repository and therefore recalls nothing** — a nice illustration of why
  `ExecutionConstraints` rather than `Principal` is the right source. Several repos
  gets the same `None`, because "which repository is this about?" has no answer and a
  guess would be a wrong-tenant-style leak in slow motion. Task mode knows its repo and
  constructs its own recaller from `plan.repos[0].slug`.
- **What the model is told.** Three groups, not the plan's two: `### Backed by evidence`
  (a person said so / a check passed — `MemoryRecord.is_verified`), `### Observed, but not
  confirmed` (a tool saw it), and `### Unconfirmed — the harness has not checked these`
  (proposals, with "Treat them as hints. Do not skip a check, a test or a verification
  because of one."). `ACTIVE` alone would have merged the first two, and merging "a
  passing test" with "a tool printed this" is the exact distinction the trust order
  exists to keep. Every line carries its provenance label and the date it was learned,
  under a heading that replaced `## Recalled from previous work (verified)` — a heading
  that asserted the very thing the lines sometimes cannot support.
- **That the log can rebuild the prompt.** `memory/recalled` is appended with
  `surface=SurfaceOp.APPEND` and carries the query, the ids, the scope ref, the count
  **and the rendered text**. `runtime/projection.py` renders it from the recorded text
  rather than re-rendering it from the ids, so revoking, expiring or superseding a
  memory later cannot retroactively change what a replay shows the model — the log is
  the record, not a cache. Nothing is emitted when the recall is empty: an event for
  "no memories" would make it indistinguishable from "this feature is not wired".
- **That it costs nothing.** There is no extra model call; `test_recall_costs_no_extra_model_call`
  pins the request count. The block is an ordinary logged message, so `runtime/context_size.py`
  measures it via `derive_messages` and compaction trims it like anything else — the budget
  mechanism is a count cap (`DEFAULT_RECALL_LIMIT = 5`), not a second accounting system.

The dead plumbing came out with it: `compose_task_brief`, `ChatTurn` and `prepare_chat_turn`
each had a `memories=` parameter that always received `()` and would have rendered recall
*twice* had it ever been filled. One renderer, one audit trail.

**Two defects the first real reader found, both fixed:**

1. **`MemoryStatus.PROPOSED` was unreachable.** `recall_verdict` had
   `if not record.provenance.is_recallable: return RecallVerdict.SKIP  # belt and braces`.
   `admit_provisional` refuses a recallable kind *by construction*, so every provisional
   record has `MODEL_ASSERTED` provenance and the check skipped all of them — a status
   listed in `RECALLABLE_STATUSES`, documented two paragraphs above as "recallable *as a
   proposal*", that no query could ever return. Phase B's suite missed it because it read
   `memory.records` instead of `recall`. It is now
   `if record.status is MemoryStatus.ACTIVE and not record.provenance.is_recallable: SKIP`
   — the belt stays for the contradictory case it was written for (an *active* record with
   no endorsement), and proposals are served. Found by `KeyError: 'prop-3'` in the renderer
   test, which is a good reminder that a renderer is a reader and readers find this class
   of bug.
2. **The text filter would have recalled nothing, forever.** The rule was
   `if query.text and query.text.lower() not in record.candidate.text.lower(): SKIP` —
   substring containment, written in Phase B against tests that passed a phrase *lifted
   from the memory itself* (`text="retry"`). The first real caller passes a **question**
   ("why does the check run before the tests in this repository?"), which no memory text
   contains verbatim, so every recall would have returned empty and looked like an empty
   store. `matches_text()` now compares token sets (`text_tokens` = the same NFKC/casefold
   folding `normalize_subject` uses, minus a small `_QUERY_NOISE` set), and an empty or
   noise-only query means *no* text filter — which is what the Phase B suite depends on.
   Over-recalling is the deliberate error direction: a recalled line is labelled and cheap,
   a missing one is invisible.

**Phase E measured.** The counterfactual is a measurement, not a claim: with
`recall_for_turn` short-circuited to `return ()`, `tests/memory/test_recall.py` gives
**`6 failed, 9 passed`** — including `test_memory_from_session_a_reaches_session_b_s_prompt`,
which is acceptance row §八-1 (session A distils and passes its check; session B's prompt
carries the line; `memory/write` is in A's log and `memory/recalled` in B's). With the
recaller restored: `15 passed`, `tests/memory/test_recall.py tests/contract/test_memory_model.py`
→ `45 passed in 2.15s`.

**Phase E gaps, stated:** matching is lexical only — no embeddings, no semantic index, and
that is a decision rather than an omission (a vector store is a dependency and a per-deployment
choice, and the seam is where one would attach); recall happens once per turn, so a memory
written *during* a turn is not shown until the next one; and the block is not re-checked
against the workspace between the recall and the model's use of it — the heading says so,
which is the honest version of a guarantee this code cannot make.

## Phase F — what stands, what is replaced, what is forgotten

A store that only ever grows answers "what did we learn" and never "what do we currently
believe". Phase F adds the second answer, in `seams/memory.py` (so both stores share one
rule) plus `runtime/memory_lifecycle.py` (so the *authority* to remove something lives
outside the store).

**Three acts, and they are not the same act:**

| Act | What it is | Authority |
|---|---|---|
| a resolution (`reconcile`) | bookkeeping between records that already exist | none — no store content is destroyed, and the losing record stays readable |
| forgetting (`revoke`) | the record stops being served; the text and the audit stay | `Operation.FORGET_MEMORY`, `ask_operations` by default |
| purging (`purge`) | the content is **really deleted**, a tombstone remains | `Operation.PURGE_MEMORY` plus a typed confirmation at the CLI |

Order is fail-closed everywhere: authorise → act → log. A refusal writes no
`memory/revoked` / `memory/purged` event, so "it was never asked" and "it was refused"
cannot be confused in the log, and `ForgetOutcome.asked` means *a person saw the question*
— an approval service with no answerer wired returns `UNAVAILABLE`, which is a refusal.

**Which records compete.** `COMPETING_STATUSES = {ACTIVE, CONFLICTED}` — **not** the
recallable set. A `PROPOSED` record is a candidate, not a belief the harness holds: it can
neither outrank a claim nor be retired by one. Letting it be superseded would silently
retire something a person might still confirm, the recall side already renders proposals in
their own block, and (all three of `SUPERSEDED`/`REVOKED`/`EXPIRED` being terminal) it would
have needed a new legal transition. This was found while writing the tests, not while
designing the phase.

**The order, and the one place it lives.** `TRUST_ORDER` (`HUMAN_VERIFIED > TEST_VERIFIED >
TOOL_OBSERVED > MODEL_ASSERTED`) with `trust_rank`/`strength_of` as the total order; a
strictly more trusted *kind* wins, then the later record, then — equal on both — a
`CONFLICT` with `winner is None`, both records kept and marked `CONFLICTED` (still
recallable, in the unconfirmed block). `plan_resolution()` is the shared order of
application, so a store decides nothing: it finds neighbours, calls it, persists. Records
are compared by their own `created_at` because an `EventRef` is a position in a log, not a
time — inventing an order for cited events would be worse than comparing admissions, and
the approximation is written down in the docstring.

**Lexical neighbours only.** `conflict_key` is `(tenant, scope, scope_ref, type,
normalized subject)`, and `subjects_are_related` (Jaccard over `text_tokens`, threshold
`0.6`) reports a `RELATED` resolution **without changing any status**. That is a deliberate
deviation from the plan's "词面相似度兜底": fuzzy matching is good enough to tell a person
two subjects look alike and not good enough to demote a record the harness verified.

**Expiry is per type, and something reads it.** `MEMORY_TYPE_EXPIRY_DAYS` gives `PLAN` and
`TASK_SUMMARY` 90 days and nothing else a deadline, an explicit `candidate.expires_at`
wins, and `expiry_for()` is applied *inside* the record factories — a policy table nothing
consults is exactly the defect shape this repository keeps meeting.

**Where the comparison runs:** on the write path, right after `memory/write` in
`runtime/distillation.py`. Reconciling at recall time would put a store write in front of a
model that is waiting and would make a turn's context depend on store read order.

**Phase F measured, counterfactual included.** Making `resolve_against` prefer the newer
record (`if incoming_rank != incumbent_rank and False`) turns
`tests/contract/test_memory_model.py tests/memory` from `100 passed` into **`4 failed, 96
passed`**: `test_a_conflict_names_both_records_and_a_supersession_names_the_winner`,
`test_a_later_claim_does_not_replace_the_one_that_was_checked` (both stores) and
`test_a_resolution_changes_what_the_next_turn_recalls` — i.e. a lower-trust conclusion really
does take over when the order is removed, which is the property the phase exists for.
`test_a_better_claim_written_later_replaces_the_weaker_one_and_says_so` still passes in the
counterfactual; it does not distinguish the two rules and is not the evidence.

**One trap worth carrying forward:** identical text is one record — `content_hash_of`
de-duplicates — so a test *about conflict* must use different wording, or it silently
asserts nothing (`admit_provisional` of a claim already stored likewise promotes the
existing record rather than filing a competing proposal).

**Phase F gaps, stated:** no embeddings, so two paraphrases of one subject are reported as
unrelated unless their tokens overlap, and no merge of two `CONFLICTED` records — a human
resolves it by confirming one; `purge` leaves a tombstone by design, so "purged" is
supported as a *fact about the log*, not as an absence; and the DB rows for a purged id are
deleted rather than blanked, because blanking `record_json` alone would leave the indexed
columns (subject, tenant, type) behind — the very thing a purge is asked to remove.

## Phase G — the two eval arms, and the producer the eval never had

`evals/` gained `no-memory` (recall off) and `no-distill` (distillation off). They are the
first arms whose switch is not a project override: `Arm.memory` / `Arm.distill`, reported
through `H.arm_changes` so a row still carries its own evidence. **The interesting finding
was not the arms — it was that the eval had wired `run_task(memory=...)` from Phase E and
nothing in `evals/` ever called `distill()`.** Every memory arm therefore began with an
empty store, `full` and `no-memory` sent the same prompt, and the comparison was guaranteed
to agree. A mechanism switched off and a mechanism never switched on are indistinguishable
from a rate, which is this repository's oldest defect shape in a new place.

Both halves are real now: a durable per-`(task, arm)` SQLite store plus the distillation
watermark (`H.memory_rows` / `H.memory_dir`, reset on `rep == 1` so rep 2 is warm and two
arms never share one), and a distillation pass **after** the checks — it is a maintenance
pass over a finished session and must change what the *next* attempt starts from.

**Measured live on `bug-fix`, 3 arms × 2 reps, the numbers and the reading are in
`docs/notes/evals.md` and `evals/README.md`.** In one line: the pass rate did not move
(6/6), and the mechanism did — `full/rep2` began with 6 records and served 5 lines into its
prompt, `no-memory/rep2` had 5 records and served 0, `no-distill` wrote none and was cold in
both reps.

**Phase G gaps, stated:** the comparison is one task at n=2, so it supports *"the arms
differ in the way they claim"* and **not** *"memory improves accuracy"* — that needs a
sweep over all eight tasks on a host whose spawned tree may write a scratch root outside
the checkout (see `docs/notes/evals.md`); and the store is per (task, arm) on a local
filesystem, which is a measurement convenience, not a deployment shape.


