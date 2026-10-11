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

- **There is no tenant dimension.** `MemoryCandidate`/`MemoryRecord` have no
  tenant field, and `recall` never compares the caller's tenant. Two principals
  from different tenants granting the same repo slug would share memory. My
  cross-tenant demonstration happened to ride on a `TEAM` scope and is now
  refused for an unrelated reason — **do not read that as the tenant problem
  being fixed.** Closing it properly means `write` taking `ExecutionConstraints`
  (the rest of the seam's effectful methods do), which is a seam-signature change
  deserving its own decision.
- **`revoke()` has no callers, and `MemoryRecord.supersedes` is never set.**
  Forget/Rollback is the least-exercised phase of every memory lifecycle.
- **Distillation is gated on an unanswered question.** The design doc's R7 says
  the privacy question — session logs becoming a training/data source — must be
  answered *before* M11. Distillation is what turns those logs into something that
  crosses sessions and people, so building it first would be building the channel
  before deciding what may flow through it.

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

