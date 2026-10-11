# Memory permissions and distillation

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before touching `memory` or starting distillation**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## Memory notes (read before touching `memory` or building distillation)

**Nothing in the runtime calls memory yet.** No `recall(`, no `write(`, and the
`memory/write` event kind is declared but never emitted. The seam and its
in-memory provider are contract-tested and correct; they have **no consumers**.
That is worth saying plainly, because "the memory feature" reads as done when you
see the tests pass, and it is not.

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
- **Distillation is gated on an unanswered question.** The design doc's R7 says
  the privacy question — session logs becoming a training/data source — must be
  answered *before* M11. Distillation is what turns those logs into something that
  crosses sessions and people, so building it first would be building the channel
  before deciding what may flow through it. ⚠️ **Still open**, and Phase A/B did
  not answer it: redaction decides what may be *written down*, not who may read
  what was written. Phase C is where this has to be settled, and the honest state
  is that it is not.

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

**Who assigns `provenance` is the load-bearing design question for M11b**, and it
is not obviously the model. If the distiller both selects content *and* labels
its trust, then `MODEL_ASSERTED` is defeated by relabelling. The rule to keep:
**the harness reads provenance off the event that evidences it** — a passing
`task/check` is `TEST_VERIFIED` by construction, a successful write `tool/result`
is `TOOL_OBSERVED` by construction — and the model may only propose candidate
*text*, never its own trust level. Related: `TOOL_OBSERVED` must not conflate
*"a tool did this"* with *"a tool saw this"*. A `read_file` result is evidence
that someone wrote those bytes, not that they are true; if distillation records
both as `TOOL_OBSERVED`, reading a malicious file launders it into memory.


