# Memory permissions and distillation

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before touching `memory` or starting distillation**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## Memory notes (read before touching `memory` or building distillation)

**The write side now has a caller; the read side still has none.** M11b Phase C runs
`memory.write`/`admit_provisional` from `runtime/distillation.py` and emits
`memory/write` for the first time — but **nothing calls `recall(` yet**, so memories
are recorded and never read back. The seam and its providers are contract-tested and
correct; the retrieval half arrives with Phase E. That is worth saying plainly,
because "the memory feature" reads as done when you see the tests pass, and half of it
is still absent.

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
| `subject` | conflict detection (Phase F), and half of the de-duplication key |
| `content_hash` | one record per claim, however often distillation runs |
| `tenant_id` | the outermost audience, checked before the repository |
| `recall_count` / `last_recalled_at` | "was this ever used" — nothing else can answer it |
| `expires_at` | a plan stops being current; a convention does not |

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


