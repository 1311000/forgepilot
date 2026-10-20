# The agent loop, the model layer, budgets and the tool surface

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before changing the loop, a provider, identity, the tool registry or a budget**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## Orchestration notes

The two architectural invariants both land here, and both are asserted rather than documented.

- **Not every request goes through the model.** An incomplete ticket is rejected by *rules*, a plain query skips classification entirely, and a low-confidence or absent classification asks a human. `ClassifierGateway` exists purely to **count** invocations — `test_an_incomplete_ticket_never_reaches_the_classifier` asserts zero, and uses a classifier that *raises if called* so the assertion cannot pass by accident.
- **The model may only narrow.** This is stronger than "intersect at the end". Candidate tools come from two sources that have nothing to do with the model — the project's configured tool set and the caller's constraints. A classification is used for exactly one thing: **choosing which configured set applies**. It cannot contribute a tool name; there is no branch that reads one from it. So even a fully attacker-controlled classification can only pick the wrong *pre-approved* set, never reach a tool the caller could not already have used. A category nobody configured falls back to the default set, **never to everything**.

**The trusted/untrusted split in context assembly is a security boundary, not formatting.** System sections are built from project config and constraints only; ticket text and the classification go in the *user* turn. A classifier copies ticket text into fields like `symptom` — if those landed in the system prompt, injected text would speak with the harness's voice and would arrive already privileged. Pinned by `test_ticket_text_never_reaches_the_system_prompt`, which puts an injection in the ticket and asserts it is absent from the system prompt and present in the brief.

The scope section tells the model what it may do, and says out loud that this is **information enforced independently** — a prompt is never a permission policy.

Other decisions worth keeping:

- **Only deterministic preprocessing decides a ticket is ready.** No model is consulted to decide whether a model is needed.
- **Only known repositories are recognised in ticket text.** Extracting an arbitrary `owner/repo` would let a ticket name a repository nobody authorised, and the router would treat it as in scope.
- **`project.yaml` is an invention, not a reading of the doc.** The doc calls for a "fixed build/test/repair workflow" without defining one; `build`/`test`/`lint` plus a retry policy is a reasonable guess that should be confirmed against what the teams actually use.
- **A configured tool name that is not in the registry is reported**, not dropped — otherwise a typo in `project.yaml` reads as "this project simply does not use that tool".
- **The classifier stub always declines.** A keyword heuristic pretending to classify would look like it worked and would blur the distinction the seam exists to prove. Declining makes the fallback path the normal path, so it gets exercised from day one.

## Running it locally

**Nothing secret belongs in a file in this repository.** `configs/` is committed, so a key written there lands in git *and* in `--dump-config`. Everything sensitive is read from the environment — either a real environment variable or a gitignored **`.env.local`** at the repo root, which `forgepilot` loads at start-up. The real environment always wins over that file, so an explicit `export` still takes precedence, and the loader returns variable *names* only so nothing can accidentally print a value.

Variables:

| Variable | Purpose |
|---|---|
| `FORGEPILOT_LLM_API_KEY` | the model credential, named by `api_key_env` in the profile |
| `FORGEPILOT_LLM_BASE_URL` | endpoint, e.g. `http://gateway.internal/v1` |
| `FORGEPILOT_LLM_MODEL` | optional; defaults to `gpt-4o-mini` |
| `FORGEPILOT_SIGNING_KEY` | HMAC key for `ExecutionConstraints`; any stable random string |
| `FORGEPILOT_DEV_TOKENS` | JSON array of dev credentials (see below) |
| `FORGEPILOT_TOKEN` | which credential to present; or pass `--token` |
| `FORGEPILOT_HOME` | where sessions, worktrees and profiles live (default `~/.forgepilot`) |

`FORGEPILOT_DEV_TOKENS` is a JSON array; each entry becomes a principal and its grant::

```json
[{"token": "dev-token", "principal_id": "alice", "tenant_id": "acme",
  "teams": ["team-a"], "operations": ["read_code", "write_code", "run_tests"],
  "repo_prefixes": ["git.corp/team/*"], "writable_paths": ["src"]}]
```

A malformed table raises rather than yielding an empty one — "nobody can log in" is much harder to diagnose than a parse error.

⚠️ **`.env.local` must be UTF-8, and this is not a formality.** PowerShell's `>>`
writes **UTF-16**, so one appended line leaves a mixed-encoding file. NUL bytes
are valid UTF-8, so the loader used to read the line as a *name* of
`F\x00O\x00R\x00G\x00…` and hand it to `os.environ`, which raised
`ValueError: embedded null character` — from inside `main()`, before any command
ran, naming neither the file nor the encoding. `env.py::_require_utf8_text` now
refuses a NUL byte up front and says which file, what is wrong and how to append
correctly. (Two corrections are recorded here for a reason: the first description
of this bug in these notes claimed the line was *silently skipped*, which was a
different bug. It was measured afterwards.)

```bash
# Git Bash — correct as written
echo 'NAME=value' >> .env.local

# PowerShell — the encoding is not optional
Add-Content -Path .env.local -Value 'NAME=value' -Encoding utf8
```

Note that `configs/` is a **template**. A real deployment's profile lives under `$FORGEPILOT_HOME/profiles/<name>/`, outside the repository.

## The model layer: what is verified, and what is not

**Verified against a live endpoint** — DeepSeek, model `deepseek-flash`, via the default profile. Confirmed for real: streaming with `stream_options.include_usage` returning usage, tool calling, `reasoning_content`, and an end-to-end turn that read a file and identified a planted bug.

Still unverified, stated so nobody assumes otherwise:

- **an internal gateway or proxy.** Nothing has been through one. Buffering, chunked-encoding quirks and auth redirects are the usual ways a working provider stops working behind a corporate network.
- **long multi-step runs.** The verified turns were one and two steps; the step ceiling and budget guards have only been exercised against scripted models.
- **any other endpoint or model.** The provider is written to the wire format, not to DeepSeek.

## DeepSeek specifics worth knowing

- `deepseek-flash` is a **reasoning model**: it emits `reasoning_content` before `content`, and with a small `max_tokens` it can spend the entire budget thinking and return empty `content`. That is not a bug in the provider — raise `max_tokens`.
- Reasoning is recorded as a log-only `assistant/reasoning` event. It is model *output*, so it is not covered by *model-visible means logged*, but discarding it would lose most of what a reasoning model did.
- `deepseek-chat` also works and is non-reasoning; useful as a control when diagnosing.

## Agent loop notes

- **The conversation lives only in the session log.** Each step re-derives its request via `projection.build_request`; the loop keeps no message list of its own. A cached `messages[]` would be a second source of truth and would drift the first time anything edited one without the other.
- **Tool results are fed back, and so are refusals.** A refused call still produces a `tool/result` the model sees — otherwise it asks again. Pinned by `test_a_refused_tool_still_enters_history`.
- **Termination is a closed set with reasons**: `completed`, `budget`, `cancelled`, `error`, `interrupted`. `max_steps` bounds *looping* while the budget bounds *cost* — a model calling a cheap tool forever stays inside a healthy-looking budget.
- ⚠️ **`max_steps` means `max_steps` steps, and it took a test to make that true.** The ceiling used to be checked *inside* the loop as `if step == max_steps - 1: break`, one iteration early — so the last permitted step never ran and a ceiling of 4 produced 3. `DEFAULT_MAX_STEPS = 12` had always meant 11. It is now the loop's `else` clause, which fires only when `range` was exhausted rather than broken out of; every other exit (completion, cancellation, an exhausted budget) is a `break` and says so for itself. The old test wrote `<= max_steps`, which passes either way — the same shape as the sandbox test that asserted a `--` was present because test and code shared the wrong assumption.

## Budgets are counted, not merely checked

Four dimensions — `tool_calls`, `tokens`, `cost_usd`, `wall_seconds` — are read by the budget guard and by the loop's between-step check. For a long time **two of them had no writer at all on any path**, and **two of them were counted into the wrong object on the path a person uses**. All four are live now, and the shape of how they rotted is worth more than the fix.

### ⚠️ Two trackers, and only one of them was watched

`ForgePilotAgent.tracker` defaulted to a fresh `UsageTracker` and `ForgePilotAgentLoop.create()` never passed one, so `turn.py` counted into an object the agent owned alone. The budget guard — built by `open_request_scope` — reads the **scope's** tracker. Two empty trackers compare equal and behave worlds apart, and the failure is silent in the direction that matters: the guard refuses nothing.

⚠️ **And it did not look broken, because half of it worked.** `turn.py` also checks the budget between steps, against the tracker passed to `run_turn` — which on the agent path *is* the agent's. So turns still stopped; only the guard, the thing that refuses an individual tool call, was dead. `run_task` wires the tracker by hand and was correct all along, which is exactly why nobody saw it: **the one path with a whole-task test was the one path that was right.**

The fix is that the scope **provides** the tracker under `USAGE_SERVICE` and `create()` resolves it. ⚠️ **And providing it is not enough — it has to be in `isolate_services`'s list**, or the child's `provide` leaks to the root and the second concurrent request dies with `service "usage" has been registered at <root>`. `test_request_scopes_do_not_see_each_other` caught that immediately, which is what it is for.

### ⚠️ `cost_usd` and `wall_seconds` had no writer anywhere

`grep -rn "cost_usd" src/` returned the guard's two reads and the value object's field, and **no assignment**. Two of the four limits could not be reached by any run, and the guard is the half the code calls non-decorative — *"a guard rather than a hook because exceeding a budget must be final."*

- **`wall_seconds`** is measurable with nothing new: accumulated in `turn.py` beside the token count, from `time.monotonic()` around each step. Accumulated rather than assigned, because a scope outlives a turn in `--chat` and the budget is per scope.
- **`cost_usd`** needs a price, so it is produced by the thing that knows the model. `openai_compat.apply_price` fills in `Usage.cost_usd` from `price_per_1k_input` / `price_per_1k_output` on the `llm` row — the same kind of deployment fact as `model` and `base_url`, which is why it lives on the same row. Attaching it to the `Usage` also means every caller gets it free, including the compaction summariser, which reaches the model through `guarded_complete` and would otherwise be invisible spend.

⚠️ **A cost budget with no configured rates is still unreachable, and that is stated rather than hidden.** Zero rates return the usage untouched: a deployment that has not said what a token costs has not said what the call cost, and inventing a figure would make the limit fire on a number nobody supplied. It is why the rates belong on the same row as `model` — `--dump-config` is where to look when a cost limit never fires.

### Why the tests did not catch any of it

Both budget tests **seeded the tracker by hand** — `tracker.tool_calls = 10_000` — and asserted the guard refused. That proves the guard *reads* the field. It cannot prove anything *writes* it, and nothing did. And every rig in the suite builds the tracker and the guards together, which is the one arrangement where the two-tracker defect cannot appear.

`tests/loop/test_the_budget_is_counted.py` builds the scope the way production does (`assemble_session`, then `root.agents.build("forgepilot", spec, runtime.scope.ctx)`) and never seeds anything. The identity test asserts `is`, not `==` — two empty trackers compare equal and behave worlds apart.

### The same number, measured per task (P3)

The guard enforces a budget *within* a turn; `evals/ceilings.py` records what each eval task
is allowed to spend *across* a run, and `evals/run.py` writes both the verdict and the bound
into the row. It is the same unit on purpose — a call count — so a ceiling in the eval and a
ceiling in a deployment mean the same thing, and a scripted run reaches the same number on
any machine. Ceilings are `1.5 x p95` of the 782 live rows measured through 2026-10-19,
with `incomplete` at zero; when a ceiling and a good run disagree, the ceiling moves in a
commit that says why. `tools/bench.py` is the other half: p50/p95 of a scripted task
end to end, with a loose weekly bound that catches the harness becoming pathological rather
than a busy runner. See `docs/notes/evals.md`.

- **Cancellation is cooperative**, stopping at a step boundary. Killing mid-tool would leave a call with no result and force the recovery sweep to invent one.
- **Context overflow fails loudly** (`ContextWindowExceeded`) rather than truncating — and it is now the *last* resort rather than the first. `run_step` compacts before it builds the request, so the loud failure only happens when compaction was tried and did not free enough. See `docs/notes/context-and-session.md`.
- **Tool-call deltas are keyed by `tool_index`, not arrival order.** An endpoint may interleave fragments for several calls; concatenating in arrival order would splice one call's arguments into another's and still parse as JSON often enough to be missed.
- **The engine is selected by key** (`ctx.agents` → `"forgepilot"`). Only one factory ships; registering a second is what makes swapping engines a config line, and `test_the_factory_registry_selects_an_engine_by_key` exercises the path.

## `materialize` vs `materialize_async`

`ResolvedConfig.materialize` is **synchronous only**. It works outside an event loop because the kernel spins a temporary one to completion; inside a *running* loop it returns immediately with fibers that have not loaded, and every service lookup afterwards yields `None` — silently. **Async callers must use `materialize_async`.** This bit the `--chat` command during M6; both forms are pinned by tests in `tests/config/test_layers.py`.


## Identity notes

- **`isolate_service`, never bare `isolate`.** See kernel note 9. A request scope opened the wrong way leaks its `constraints` to the root, turning the fail-closed property into decoration — and it fails silently, so nothing would tell you.
- **The fail-closed property is the point of M5.** A plugin declaring `inject = ["constraints"]` registered at the root stays `PENDING`; registered inside a request scope it goes `ACTIVE`. Forgetting to establish authority yields a plugin that does nothing rather than one that acts unbounded. Pinned by `test_a_plugin_needing_constraints_cannot_load_outside_a_scope`.
- **`PolicyService.decide()` refuses non-decision inputs at runtime**, not just by annotation. Passing a dict (a stand-in for a ticket understanding or a plan) raises `TypeError`. Python does not enforce annotations, so the check is what makes "the model proposes but never decides" real.
- **Patterns are not sets.** `derive` cannot intersect repo *prefix* sets: `{"git.corp/team/*"} & {"*"}` is the empty set, so a permissive policy would *revoke* a specific grant — the failure this function exists to prevent, inverted. Prefixes and paths are narrowed by coverage (`_prefix_covers`), not by set intersection. This was a real bug caught by a test.
- **The signature is dropped by `narrowed_to`.** A narrowed set has different content; keeping the signature would mean a narrower set carrying a signature over a wider one — the exact forgery signing exists to catch. Whoever narrows must re-sign.
- **Signing needs `FORGEPILOT_SIGNING_KEY`** (environment, not config, so it never lands in `--dump-config`). Missing key is `SigningKeyMissing`, not "skip verification".
- **An unsigned set is refused**, not treated as "unverified but probably fine" — otherwise omitting the signature would be the easiest way past the check.
- Signing narrows the forgery surface; it is **not** the whole boundary. In-process it cannot stop code calling a tool with no constraints at all. The defences against that are the seams re-checking and the request scope.

## Tool pipeline notes

The pipeline is `tool/call` → pre-execute → approval → guards → arm → execute → post-execute → finalize → `tool/result`. Four things about it are load-bearing:

- **`tool/call` is flushed, not just appended, before the tool runs.** Buffering it would defeat the point: the guarantee is that a crash *while the tool is running* still leaves a log saying the call happened.
- **Nothing effectful happens before approval.** The privileged phase is `arm` and it runs after both approval and the guards. That ordering — not any scope object — is what makes DSH #1863 structurally impossible here.
- **Pre-execute runs an explicit ordered chain, not a Cordis waterfall.** A deliberate departure: the port's waterfall does not thread values forward (`next(value)` discards its argument), so a decision could only travel by return value, and a listener that forgets `next()` silently ends the chain. First-opinion-wins is what a permission check actually wants; the middleware machinery buys nothing and can fail quietly.
- **A guard can only abstain or deny.** `GuardDecision` has no `allow` member, so no guard can re-permit what another refused. Adding one would turn the security boundary into a suggestion. Guards are also *snapshotted at `tool/call` time*, so a plugin registering or removing one mid-call cannot change that call's verdict.

Two fields exist specifically so that policy is enforced rather than merely recorded:

- **`ToolDefinition.required_operation`** maps a tool to the `Operation` policy speaks. Without it `permitted_operations` was recorded, snapshotted, shown in `--dump-config` and **consulted by nothing** — the most dangerous kind of security field, because it looks enforced. Declared, not inferred from `effects`: "writes a file" and "opens a PR" can share an effect while needing different permission.
- **`ToolDefinition.describe_targets`** extracts normalised `ResourceTargets` (repos, paths, commands) so guards never parse argument JSON. One place knows that `{"path": ...}` is a path. **Each field now has a reader**: `repos` → `repo-scope`, `paths` → `path-scope`, and since M14 `commands` → `command-scope` (`command_scope_guard` in `runtime/guards/builtin.py`), which is the field that used to be extracted and dropped (see `docs/notes/defect-shape.md`).
- **The command guard is the one guard that is opt-in, and it reads the *line*.** It abstains unless `ExecutionConstraints.allowed_commands` is a declared set (`None` = no policy, `frozenset()` = nothing permitted), because a guard that refused every `run` for every existing deployment would be a breaking change wearing a security rationale. It decomposes the shell line into the programs it would start (`runtime/commandline.py::programs`) and refuses a line it cannot decompose — but it does not inspect *arguments*, and it never sees `run_checks`, whose build/test/lint lines are human-written and go straight to the sandbox from `runtime/repair.py`. Which programs may start is the guard's question; what they then do is the sandbox's.
- ⚠️ **`split_command` is platform-aware by design, so a test that pins a Windows spelling is a Windows test.** `split_command` uses `posix=True` everywhere but Windows, because a backslash *is* an escape to `sh`; on CI (`ubuntu-22.04`) the line `C:\tools\Python\python.exe -m pytest -q` really tokenises to `C:toolsPythonpython.exe`, and the first version of `tests/security/test_command_guard.py` — which asserted the Windows reading on both platforms — **went red on CI while passing on this box**. The guard's answer is fail-closed in *both* readings (a name the list does not contain is refused, never waved through), so the test now asks `split_command` which reading this platform is in instead of assuming one, and the portable assertion uses `/usr/bin/PYTHON`. The first repair then went red a second time, for the same reason one level down: it typed the *escaped* token from memory (`C:toolsPythonpython.exe`) and got a character wrong — a literal that never executes on the box you develop on. **Assert the property (`normalise_program(token) != "python"`, and the guard denies), never the platform's output.** The same lesson has a third form, and it cost the same afternoon: **a bound calibrated on the developer's dirty tree.** The anti-vacuity guard in `tests/contract/test_the_tree_is_case_exact.py` first asserted `len(FILES) > 400`, measured here at **442** — and CI walks **347**, because the difference is untracked, gitignored output under `evals/results/`. A guard whose whole job is to notice an *empty* walk does not need a size at all: it now asserts that one tracked path from each of the five trees is present, which no checkout can be missing.

## The tool surface: four tools, and why `edit_file` was the missing one (P2-1)

`read_file`, `write_file`, `run`, `code_search` — and until P2-1 there was no
targeted edit. `write_file` is a whole-file overwrite, so changing one line of an
eight-hundred line file cost the model the entire file in output tokens, and its
failure mode is **silent**: a dropped or truncated line reads as a successful
write. Every agent that has published on this ships a targeted edit for exactly
that reason (Claude Code's `Edit`, Aider's five model-matched formats, OpenHands'
`str_replace_editor`), which is why this was ranked the single most direct drag on
end-to-end success.

`edit_file(path, old_string, new_string, replace_all=False)`. Three rules, each
because the alternative fails quietly:

- **The search text must be found**, or nothing is written and the refusal names
  the likely cause (indentation). A near miss written as a match is the worst
  outcome available.
- **It must be found once**, unless `replace_all` says otherwise. An ambiguous
  substring is the *normal* case in real code, and both alternatives are bad —
  replace the first (silently edits a site nobody mentioned) or replace all
  (silently edits sites nobody mentioned). `_uniqueness_refusal` names the count and
  both ways forward.
- **An empty `old_string` and an exact no-op are refused.** An empty search matches
  at every position, so it would insert the text throughout the file.

Two implementation notes that are load-bearing rather than tidy:

- ⚠️ **It reads the file the way `read_file` does** — raw bytes, no newline
  translation. `read_text()` applies universal newlines, so a model that copied its
  `old_string` out of a `read_file` result would miss on every CRLF file, and the
  miss would look like the model's mistake. **The tool that shows you the text and
  the tool that searches it have to agree about what the file is.**
- ⚠️ **The second attempt is a real match, and the splice is into the original
  bytes.** If the exact search misses, the match is retried on a flattened copy
  (a map from flattened offsets back to original ones, built rather than derived —
  the substitution changes length, so an offset in the flattened text is *not* an
  offset in the file). The replacement is spliced into the untouched original, so
  the text nobody asked to change stays byte-identical: the difference between a
  one-line edit and a whole-file rewrite in review.
- The inserted text takes the file's own line endings, and the file is written with
  `newline=""` — the default translates `\n` to `os.linesep` on the way out, so a
  CRLF file would come back as `\r\r\n`.
- **An oversized file is refused rather than truncated.** `read_file` truncates
  because showing part of a file is useful; *writing* part of a file destroys the
  rest, and "it edited a big file" and "it deleted the tail" would produce the same
  success message.

`edit_file` declares `{READ, WRITE}` — it reads the file to match against, and `arm`
intersects the declaration, so a narrower `{WRITE}` claim would grant an edit on a
call that cannot read.

⚠️ **Registered is not reachable.** `project.yaml`'s `tool_sets` are what the router
hands the model, so `edit_file` was added to the writable set of the template and
both `examples/stat-fix*/project.yaml` — and
`tests/tools/test_edit_file.py::test_the_shipped_project_offers_it_to_the_model`
pins it, because a tool nobody is shown is dead code. That is the same defect class
this file records twice already (a plugin row naming a module nobody wrote, a field
consulted by nothing).

Capabilities are minted at `arm` as the intersection of three independent limits — declared effects, caller constraints, and **what the host can actually enforce**. `exec` is refused outright when sandbox enforcement is weaker than `minimum_enforcement_for_exec` (`FULL` by default), so shell tools refuse on a host reporting `partial`. That is the correct outcome, not a gap to paper over; lowering the requirement is possible and visible at the call site.


## `list_dir` and `glob`, and why discovery is now explicit (P5-1)

Two tools exist because of what M28 measured rather than because a mature agent has
them. The `no-shell` arm removed `run`, and the agent still found files it was never
told about — by asking `read_file` for a **directory**, which answered with a
listing. Discovery worked, and it worked through a tool whose contract is "one
file" and whose error message happened to be helpful. Explicit tools make the
behaviour a contract instead of an accident: `list_dir` answers about a directory
and says so, and `glob` answers "which files match".

Three details are load-bearing.

- **The root is a legitimate directory, and the path guard cannot express it.**
  `normalise_relative(".")` is `None` — the root is not a file anyone can address —
  and the guard reads `None` as an escape. So the definition's target for the
  default is *no target* (`_targets_dir`), not `"."`; a target the guard would
  refuse would have made the default unusable while looking like a permission
  decision.
- **A symlink may point out of the workspace, and the guard judged the pattern.**
  `glob` resolves each match and drops the ones outside, rather than returning a
  path whose content the tool would then read. Dropping is the only answer that
  neither leaks the target nor claims the file is not there.
- **Both are bounded and say when they were cut.** A listing or a glob that
  silently stops is a wrong answer: the model concludes the rest is absent.

⚠️ `read_file` still lists a directory in its error. That is deliberate and now
redundant rather than harmful: the message names the entries and points at
`list_dir`, and removing it would break a shape the M28 logs depend on.

## `apply_patch`, and why a batch is a decision (P5-2)

`write_file` and `edit_file` each answer about one file. A change that spans
three is one intention, and issuing it as three calls makes the state *between*
them real: if the second fails, the first is already on disk, and the next turn
reasons about a tree nobody planned. `apply_patch` takes a list of exact edits
and applies them as one decision — every `old_string` is matched against the
in-memory text first, and nothing is written unless **all** of them match.

Three details are load-bearing.

- **The refusal names the patch, not the batch.** "One of your eleven edits did
  not match" costs another turn to interpret; `patch 2 of 11: old_string was not
  found in src/limits.py (14 lines read)` does not. The count and the line count
  matter because the usual cause is a stale read rather than a wrong idea.
- **The guard is shown every path.** `_targets_patches` lists all of them, not
  the first: a batch that carried a write outside the writable set past a guard
  that only saw the path it liked would be a permission defect wearing a
  convenience API. Writability itself stays the guard's job — the tool cannot
  know the policy, and duplicating that check here would let the two disagree.
- **Patches to one file apply in order, and see each other.** That is what lets
  a caller express "rename this, then use the new name" in one call. The
  alternative — a snapshot per patch — would make the second patch match text
  the first had already replaced, which is the same class of silent-wrong the
  tool exists to prevent.

The write phase stages each file beside itself (`.<name>.forgepilot-patch`) and
then `os.replace`s it, so a file is never visible half-written and a rename
cannot fail on a full disk. Rename is atomic *per file*, not across files: if a
later rename fails, the message says which files were already written rather
than claiming a rollback that did not happen. What the tool guarantees exactly
is the matching phase, which is where a batch is most likely to go wrong.

`edit_file` and `apply_patch` share `_splice`, so "exact match or refuse", the
ambiguity message, the CRLF normalisation retry and the rule that inserted text
adopts the file's own line endings cannot drift between them.
