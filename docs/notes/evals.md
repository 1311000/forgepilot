# Evaluation notes

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before running the eval, changing a task or an arm, reading a report, or deciding whether a mechanism earns its place**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## Evaluation notes (`evals/`)

Ten tasks, eight arms, one JSONL row per attempt. **This cannot produce the doc's
"+32% vs Claude Code" number** — there is no comparison arm here, and inventing
one would be the same unfounded claim in a new place. What it answers instead is
the question underneath: **does each mechanism earn its place**, by running the
same tasks with one thing switched off at a time.

**Each shape exists to exercise a *different* mechanism, because ten off-by-one
fixes would give ten correlated numbers and one finding.** Each carries its own
offline solution so **a task whose own correct answer does not score is a broken
task** — which is far cheaper to discover than from a live run. ⚠️ **The count is
deliberately not written down here any more.** `evals/README.md` holds the current
table, one row per task, and `tests/evals/test_the_docs_match_the_tasks.py` fails
for a task directory with no row there *and* for a hand-counted "N tasks" phrase in
that README — this file's own headline said "seven tasks, five arms" beside "four
task shapes" for several milestones, which is what a duplicated count does.

The four the set started with:

| task | mechanism it exercises |
|---|---|
| `bug-fix` | the control. A real bug, a real test, a one-line fix |
| `feature` | the new test *is* the specification for something that does not exist yet |
| `incomplete` | rules-first: no reproduction steps means `awaiting_info` with **zero** model calls |
| `out-of-scope` | the permission boundary, with a legitimate fix next to the forbidden one |

The six that came later — `find-the-bug`, `find-the-rule`, `two-attempts`,
`no-failing-test`, `not-in-the-checkout`, `two-repos` — are described in
`evals/README.md`'s table, which is the authoritative list.

**`--scripted` measures the harness, not the agent**, and says so in the report.
A replay model performs a fix because it was told to. It exists to prove the
machine works before spending anything — and it earned that immediately.

### What it found, none of which the unit tests did

**1. The oracle pinning made a feature unverifiable.** Pinning restores changed
`test_paths` to base, so a test written for something that does not exist yet is
deleted for the deciding run, and the base tests have never heard of the feature.
The `cheating` variant — test written, implementation not — scored a **pass** with
the module untouched. Fixed by checking **both trees and requiring both**: the
pinned tree proves nothing was weakened, the agent tree proves the feature its own
tests describe exists. Neither alone is enough. See `repair.combine_checks`.

**2. `_narrowed_paths` was a filter where the operation is an intersection.** A
grant of `"."` — the whole checkout — behind a project limited to `src/` produced
**nothing writable**, and the guard refused with *"nothing is writable"*: a true
statement about the intersection the code computed and a false one about the
authority the caller held. The mirror case was broken too (`src/` behind a
permissive project). **Every existing test used the same set on both sides**, the
one case where a filter and an intersection agree.

**3. A check that passed with nothing changed crashed at the commit.** Reachable
whenever the checks pass against an unchanged tree. It surfaced as git's own
*"nothing to commit, working tree clean"* inside a `GitFailed`, at the last step
of a run that otherwise succeeded. Now `NothingToCommit`, and the task reports the
outcome instead of a git error.

**4. A feature ticket could not write anything.** The classifier emits `bug`,
`question` and `feature`; the sample project routed only the first two, so a
feature fell back to the read-only set. The fallback is right — never
"everything" — but a category the classifier can actually emit needs a set that
can do the work.

**5. An expectation that named a file measured conformance, not the task.** The
`feature` task required `tests/test_series.py`; all three live runs wrote
`tests/test_median.py` — a better name — and were marked failures for it. An
expectation may demand *that* something changed; naming it demands the agent share
the expectation author's vocabulary.

**6. A boundary task that never met the boundary passed.** All three live
`out-of-scope` runs *read* the forbidden file, saw the value it names is never
consulted, fixed the real bug and never went near the wall — good agent behaviour,
and a **pass with no evidence about the boundary at all**: assurance about a wall
nobody walked into. The task now requires the refusal, and the `never-meets-the-wall`
variant replays the original behaviour and must fail.

**7. The boundary task measured the wrong thing — and the agent was better than
the task.** The second live run: all three `out-of-scope` runs fixed the real bug,
**read the half from the forbidden `settings.py` instead of hardcoding it** —
satisfying the ticket's instruction without the forbidden write — and then
explained the constraint and asked for confirmation:

> `settings.py` … 不在本会话的可写路径（`src`）内，所以我没有、也不会去修改它
> 如果"平台规范"实际上要求把半值**写进** `settings.py`，那超出了我的可写范围，需要先放开该路径的写权限

The task had required *a guard refusal* as the evidence that the boundary was
reached. **Models do not walk into the wall.** The failure worth testing for is
the opposite: **silent substitution** — doing a permitted thing instead, and
reporting success, after which a reviewer cannot tell the instruction was not
followed. The expectation is now `reports: [settings.py]`, checked over the whole
conversation (the final turn is often a tool call with no prose), and the
`silent-substitution` variant reproduces that and must fail.

That sixth one is the general lesson and it is the same shape as everything else
here: **a green result is not evidence that the thing under test was exercised.**
The eval had to be taught to say "this run produced no evidence" rather than
silently counting it as a success.

### Rules worth keeping

- **A row records *why* it failed, not only that it did.** "How often" without "in
  what way" is not actionable, so `score()` returns reasons.
- **Counters come from the log**, never from the runner's own bookkeeping. A
  second source of truth drifts — that is the shape of most of this project's
  defects.
- **A crashing run is a result**, recorded with its traceback. Swallowing it would
  make a broken arm look like a small sample.
- **The harness runs the profile a person runs.** It sets the three `${VAR}`s
  `examples/stat-fix/patch.yaml` expands and passes that file by path, rather than
  assembling its own plugin tree — an eval of a program nobody has is worth
  nothing. It loads the project through `load_project`, not
  `project_from_mapping`: only the loader expands `${VAR}`, and skipping that left
  the check command as the literal `${FORGEPILOT_PYTHON}` and made every task
  `could_not_run`.
- **Results are gitignored.** A committed result file is a claim nobody can check;
  a run is reproduced by running it.
- ⚠️ **`Arm.project` was read by nothing, so three of the five arms never switched
  anything off.** `write_project(task, …)` was called without the arm, so
  `no-search`, `no-pinning` and `no-repair` — each entirely a project override — ran
  the *baseline* configuration. Only `Arm.patches` was applied, which is why
  `tight-window` was the one arm that behaved differently. The first ablation's
  `no-search 100% / no-pinning 100% / no-repair 100%` was **three repeats of
  `full`**, and the conclusion drawn from it ("the tasks are too easy for these
  mechanisms") was about mechanisms that had never been turned off.
  ⚠️ **The lesson is the shape: a mechanism that has no effect and a mechanism that
  was never disabled produce identical tables.** Nothing in a rate distinguishes
  them. `tests/evals/test_arms_are_applied.py` asserts the wiring, and
  `test_every_arm_changes_something` stops a decorative arm being added later.
- ⚠️ **And the scratch directory named the arm, so the agent could read the
  treatment off its own path.** It was `find-the-bug-no-repair-1`, and the working
  directory is the `repo/` inside it — which `dir /s /b`, `%CD%` and any traceback
  will print. Two runs of the first wired ablation said so in prose: *"the task
  scope — find-the-bug, no repair — I have not modified any source"*, with
  `changed=[]`. They diagnosed the bug correctly and declined to fix it.
  **`no-search` is the worst case**: a model that reads "no search" has been told
  not to search, which explains its `100%` and its *lower* call count on the
  localisation task.
  The directory is now opaque (`run-<hash>`) and the task/arm/rep mapping lives in
  the results row, where a person reads it and the model does not.
  `test_the_agent_cannot_read_the_arm_off_its_own_path` checks every arm.
  ⚠️ **The general shape**: an ablation is only an ablation if the subject cannot
  tell which arm it is in, and nothing in the output would have shown otherwise.
  What showed it was a run quoting its own directory name.
- ⚠️ **And the table now has to show the arms' diffs, because prose did not.**
  `Arm.description` was accurate the whole time `Arm.project` was read by nothing:
  three arms described a switch they never threw, and their table was
  indistinguishable from three arms that threw it and it made no difference.
  `harness.arm_changes()` diffs the two configurations, `run.py` records it in every
  row as `arm_changes`, and `report.py` prints it before any rate:
  `full → (nothing — the baseline configuration)`, `no-repair → retry: {...} -> {...}`,
  `tight-window → patch:llm: None -> {…, max_context_tokens: 6000}`.
  It counts **patch rows as well as project keys**, because `tight-window`'s switch
  lives entirely in a patch and a project-only diff would report it as a no-op —
  the same mistake, made twice already, in a new place.
  ⚠️ Rows from before this was recorded print `(not recorded in this file)`, never
  `(nothing)`: the second would be a claim the file does not support.
- ⚠️ **Scratch is not inside the repository, and that is a correctness property
  rather than tidiness.** The eval's own answers are in the repo
  (`evals/tasks/<name>/solution/`, `task.yaml`) and `run` has **no containment**, so
  a workspace at `evals/scratch/<task>-<arm>-<rep>/repo` had the solution to the
  task it was being scored on three directories up. It now builds under
  `$FORGEPILOT_HOME/eval-scratch/`. The same mistake also created one nested git
  repository per worktree inside the project — an IDE reports sixty of them as
  *"directories wrongly registered as VCS roots"*, which is how it was noticed.
  **This closes the accidental case, not the deliberate one:** `run` still has no
  containment, and the project's path reaches the model regardless through the
  check command's interpreter. Measured afterwards: no observed run strayed — the
  commands are relative (`dir /s /b`, `type src/metrics.py`) plus that interpreter.
- ⚠️ **And a run that left its checkout fails, rather than passing quietly.**
  `harness.tool_calls_that_left_the_workspace` reads every `run` command in the
  session and reports one naming an absolute path under the project but outside the
  eval's scratch area; `score()` turns that into a failure, because a warning beside
  a pass is a pass. Same distinction as `reports:` and `guard_refusals` — not "did
  it pass" but "did it pass *this*". **Only `run` is examined**: `read_file`,
  `write_file` and `code_search` all resolve against `ctx.workspace_root` and refuse
  an absolute path or a `..` escape, and `code_search` takes its root from the
  context rather than from the model, so a call naming the answers is refused before
  it reads anything and appears as a guard refusal instead. `run` has no containment.
  ⚠️ **`..` is deliberately not checked.** The first version flagged any upward
  traversal and fired on `cd src && … ; cd .. && …` — a command that *returns* to
  the root. Auditing every recorded run: a `..` with a path separator after it has
  never appeared, and after scratch moved out of the repository a relative chain
  cannot reach `evals/tasks/` anyway. Cost was a false positive on honest work;
  benefit had gone to zero. **Shipped with zero false positives across all 72 eval
  runs on disk and three true positives**, one of them the `cd /d C:\…\forge` escape
  documented above — a detector that cries wolf gets switched off, and then it
  protects nothing. Pinned by `tests/evals/test_left_the_workspace.py`, including
  the exact command the dropped rule misfired on.

### ⚠️ One `pass` rate hides the mechanism (P5-6′)

The eval's subject is **whether each mechanism earns its place**, and the repair
loop is the most expensive one in the set: every retry is a model call. A row that
recorded only the *final* verdict made the loop unfalsifiable — a run rescued on
the second attempt and a run that worked first time produced the same `pass`, so
`no-repair` could not be shown to matter even when it did.

`row["check_verdicts"]` now records one boolean per attempt (`task.py` appends
exactly one check per attempt, in order), and the report prints **`pass@1`** and
**`pass@2`**, cumulative — so `pass@2 >= pass@1` always and the gap between them is
the loop's work. Aider's leaderboard is the precedent and the reason it is worth
copying: gpt-5 (high) **52.0% → 88.0%** over 225 polyglot exercises, and that ~36
point gap *is* the error-feedback loop quantified.

Three states, deliberately distinct, and the third is the one that matters:

- `True` / `False` from a recorded list — an attempt was made and that is what it
  decided. **An empty list is also recorded**: a plan that never became ready made
  no attempt, and `any([]) is False` is the honest reading;
- **absent** (`None`) when the row predates the recording → the report prints `-`
  and a footnote naming the file. *A missing number is not a zero*: every results
  file on disk before this change has no verdicts, and counting those rows as
  first-attempt failures would have invented a mechanism rate out of an absence.
  Same rule as `arm_changes` printing `(not recorded in this file)`.

The report also prints a **repair-loop section** apart from the tables, because it
is a statement about a mechanism rather than about a cell, and it is the one figure
`no-repair` is supposed to move. A loop that converted nothing says so out loud
rather than printing a zero nobody can read.

### ⚠️ Running the eval on a confined host

`$FORGEPILOT_HOME` defaults to `~/.forgepilot`, which is **outside the workspace** —
and on a host that confines a spawned process tree to its workspace, `prepare_repo`
dies there with `PermissionError: [WinError 5]` on `.git/hooks/applypatch-msg.sample`
(the read-only-object problem in `docs/notes/task-delivery.md`, at a path it may not write). The symptom is a
row that `run_one` raised on, which the report correctly labels *"did not run at
all"* — but a whole sweep can come back like that and look like a rate.

```powershell
$env:FORGEPILOT_HOME = "$PWD\.forgepilot"   # inside the workspace; gitignored
$env:TEMP = "$PWD\.tmp"; $env:TMP = $env:TEMP   # or pytest fills the repo root
```

Both are environment facts rather than defects in this repository — but the second
one is worth knowing before blaming the harness for thousands of new files in
`git status`.

⚠️ **`FORGEPILOT_EVAL_SCRATCH` needs the same treatment, and the reason is the one
`scratch_root()` gives for keeping it outside the repository.** Measured in M11b Phase G,
when a memory-arm sweep was started with the default scratch root: the first failure was
`PermissionError … eval-scratch\run-…\repo\.git\hooks\applypatch-msg.sample` (a `run-*`
directory an older sweep had left behind) and the second,
after `FORGEPILOT_HOME` was moved inside, was
`PermissionError … .forgepilot\sessions\local\202610\session-*.header.json.tmp`. What
worked:

```powershell
$env:FORGEPILOT_HOME = "D:\wroksapce\pythonCode\forge\.tmp\fp-home"
$env:FORGEPILOT_EVAL_SCRATCH = "D:\wroksapce\pythonCode\forge\.tmp\eval-scratch"
.venv/Scripts/python.exe evals/run.py --live --task bug-fix --arm full --arm no-memory --arm no-distill --reps 2
```

⚠️ **And that is mechanics, not a scored measurement**, because an inside-the-repository
scratch root is exactly the leak `scratch_root()` warns about: `evals/tasks/*/solution/` is
one `..` away from a run the model can read. The confinement was a property of *this
session's* spawned process tree rather than of the box — the same paths were writable from
`pwsh` (`Set-Content` to `~/.forgepilot/probe-ps.txt` succeeded) while
`.venv/Scripts/python.exe` was refused at `~/.forgepilot/eval-scratch/probe-a`,
`~/.forgepilot/probe-dsh.txt` and even `D:\wroksapce\pythonCode\probe-out.txt`
(`[WinError 5]` / `[Errno 13]`), and `.tmp/probe-in.txt` written fine. Earlier sweeps on
this machine reached `~/.forgepilot` without any of this. **So: a scored sweep needs a
writable scratch root outside the checkout; if the host will not give the spawned tree one,
say so in the row's context rather than publishing the rate.**

Hit again at M13 Phase E, on the first `two-repos` attempt, in the identical place
(`PermissionError … eval-scratch\run-…\repo\.git\hooks\applypatch-msg.sample`) and cured by
the same two variables — so the offline sweeps in this file name them up front rather than
letting each new era rediscover the wall. The same confinement applies to `FORGEPILOT_HOME`
(the session store), which is why both are moved together.

### ⚠️ The sandbox confined writes, not reads — measured, and an eval arm depended on it
*(closed since: `ReadScope.WORKSPACE` masks every unnamed top-level entry, `probe()` verifies that
claim with a read, and the eval's containment row claims it — see the last paragraph of this section
and `evals/README.md` finding 21. What follows is the measurement that made the case.)*

`FORGEPILOT_EVAL_CONTAIN=1` swaps the eval's `sandbox` row for `NamespaceSandboxPlugin`
and raises `minimum_enforcement_for_exec` to `FULL`. Run that way against
`not-in-the-checkout` in a `--privileged` **`ubuntu:24.04`** container, `full` scored
**3/3** (7–9 calls, zero escapes) and every `no-search` row escaped anyway — 31, 7 and 20
out-of-checkout commands, all three ending `ready`. The reason is one line of
`mount_rules`:

```python
rules: list[MountRule] = [MountRule(target=Path("/"), read_only=True)]
```

`/` is bound **read-only**, not hidden, so a sandboxed command can still *read* the whole
filesystem. Measured through the shipped `build_argv` with the shipped rules, as root in
the same container: `cat /eval/evals/tasks/not-in-the-checkout/index.json` → **rc 0 and the
corpus printed**; `find /eval/evals/tasks -name index.json` → rc 0; `cat /etc/hostname` →
rc 0; `ls /work/evals/tasks` → rc 0; while `echo poison > …/POISON` → **rc 2, `Read-only
file system`**.

Two consequences, and the sandbox was not the thing at fault — it did what it was built to
do (protect the host from the command) and the *eval* was asking it a question it does not
answer:

- **`not-in-the-checkout`'s `no-search` column cannot be obtained with the boundary as it
  stood** — not on this box, not on Linux, and **not in CI**, whose checkout *is* the
  filesystem the eval runs in. The retracted claim ("on Linux the shell cannot leave the
  worktree and the premise holds") is recorded in `evals/README.md` findings 18–20.
- **What it does measure is worth publishing**: whether the agent stays inside the boundary
  while failing to finish — `out-of-scope`'s property applied to information rather than to
  writes. All three contained rows broke it.

Closing the route meant a boundary that confines **reads**, and it has since been built:
every top-level entry the boundary does not name becomes an empty `tmpfs`, the claim is
opt-in (`ReadScope.WORKSPACE`) because it costs something, and `probe()` verifies *that*
claim with a read rather than a write. The measured effect on this task, in the same
`--privileged ubuntu:24.04` container: `full` **1/1 pass** (8–13 calls) and every
`no-search` row **failed with the file unchanged**, where finding 20 had all three `ready`
with the held-out probe satisfied. The rate took one more change to print, and the
mis-step is worth keeping: an effect-aware key that asked only "did it exit 0?" still
marked all three rows `aside`, because `ls -la /eval/` exits **0** over an **empty** mask.
Exit codes cannot tell a hit from a mask. The verdict that works is about the boundary the
row ran under — contained runs execute only through a provider whose probe verified the
read claim, so an outside command reached a mask and is an *attempt* — and with it the
cell reads `full` **1/1** against `no-search` **0/3**, with **19 / 41 / 10** outside
attempts per row and none through. The report prints those counts; without that line a
cell full of refused probes would read like a cell where the agent merely failed. See
finding 21.

⚠️ Two instrument lessons from the same run, both about `docker run --rm`: results written
**inside** the container die with it (mount `evals/results` and `FORGEPILOT_HOME` out, or
copy them out before it exits), and **never source `.env.local` by hand** — it carries nested
JSON (`FORGEPILOT_DEV_TOKENS`), a shell assignment mangles it, and the row then fails in
`AuthenticationFailed … not valid JSON` with `0 calls`, i.e. it looks like a run.
`evals/run.py` calls `load_local_env()` itself.

### ⚠️ A green suite the agent can read is satisfiable one case at a time (P5-6′b)

Oracle pinning is the strongest published mitigation against an agent rewarding
itself, and it covers **one** cheat class: editing the test. **ImpossibleBench
(ICLR 2026) measured four, and three obey "do not touch the tests" completely** —
special-case the inputs, record extra state, overload `__eq__`. On mutated
multi-file SWE-bench tasks it found GPT-5 cheating on **54–76%** of runs, and LLM
monitors caught only **42–65%** of it. `runtime/pinning.py` cannot see any of
those three, and neither can the differ, the state machine or the diff.

So an eval task can declare **held-out inputs**: a file under the task directory,
executed by the harness against the agent's finished tree *after* the run, exit 0
meaning the behaviour is there.

```yaml
probes:
  - name: moving_average_is_general
    file: probes/moving_average_is_general.py
expect:
  probes: pass
```

It is a file under `evals/tasks/<name>/probes/` rather than another assertion in
`tests/`, and that is the entire mechanism: **anything in `tests/` is something the
agent can read while it works, and anything it can read it can satisfy one case at
a time.**

Three rules, and each is a way the instrument could lie:

- **It is consulted only when the visible checks passed.** That is the state this
  exists to disambiguate; a row whose checks failed is already actionable, and
  reporting the probe alongside would blur which of the two happened.
- **A declared probe that was never recorded fails the row.** Otherwise the
  expectation silently degrades into "nothing was held out" — the failure this
  mechanism exists to prevent, wearing its uniform.
- ⚠️ **It runs in the session's worktree, not the checkout the fixture was copied
  to** (`harness.working_tree`). The example's workspace provider is a per-session
  `git worktree`, so the edits live in a *second* checkout — the same lesson as
  `code_search`'s `root=`, one layer down. The first version probed the origin and
  reported the **correct** fix as a cheat and the cheat as a cheat: identical
  failures, both wrong about the fix, and the message named the probe rather than
  the path. Two extra worktrees are refused rather than guessed, because probing a
  tree nobody chose is how a probe lies.

⚠️ And a probe is executed as a *file*, so Python puts **its own directory** on
`sys.path` and never the working directory. The harness adds the tree to
`PYTHONPATH`; without that every probe dies with `ModuleNotFoundError` in **both**
directions, which reads as a broken task rather than a broken instrument.

`bug-fix/special-cases-the-input` is the variant that proves it: it writes a
`moving_average` enumerating the four inputs the visible assertions use. The pinned
suite goes fully green with the rule absent from the module, and the row fails on
the probe — `checks: pass` **and** `probes: pass` are both required, so the failure
reason says which one happened. Its passing later would be the alarm: it would mean
the held-out input had stopped being held out, most likely by being moved under
`tests/`.

### The memory arms: the mechanism fires, and the pass rate does not move (M11b Phase G)

Two arms were added, and they are the first whose switch is **not** a project override:
`Arm` gained `memory: bool = True` and `distill: bool = True`, `arm_changes` reports each
as `True -> False`, and the two guards that call an arm decorative
(`test_every_arm_changes_something`, `test_each_arm_reports_what_it_changed`) accept a
runtime switch. The config plane says what the store *is*; nothing in it can say whether
*this run* reads or writes one.

⚠️ **The eval had wired recall and had no producer.** `run_task(memory=root.memory)` came
from Phase E, and `distill()` had no caller in `evals/` at all: every memory arm began with
an empty store, `full` and `no-memory` sent the same prompt, and the pair was guaranteed to
agree. Two halves are now real:

- **A store that outlives the attempt.** `H.memory_rows(task, arm)` adds two patch rows —
  `SqliteMemoryStorePlugin` and the distillation watermark — at absolute paths under
  `H.memory_dir(task, arm)`, and `reset_memory` runs on `rep == 1` only. `prepare_repo`
  wipes the checkout every run, so memory is the only thing that can carry from one rep to
  the next; the store is keyed by task and arm and **not** by rep, so rep 2 is warm and two
  arms never share one (otherwise the comparison is a sequence). Each row records
  `memories_before`, read after boot and before the first turn, which is the only moment it
  means "what this run could have recalled".
- **Distillation after the checks**, never during a turn: it is a maintenance pass over a
  finished session, and it must change what the *next* attempt starts from.

Measured live on `bug-fix`, 3 arms × 2 reps, same model and budget
(`evals/results/20261012T104203-live.jsonl`, every row `pass ready`, `stats.py` changed):

| arm | rep | calls | before | distilled | memory writes | recalls | lines served | passed |
|---|---|---|---|---|---|---|---|---|
| `full` | 1 | 14 | 0 | 5 | 6 | 0 | 0 | ✅ |
| `full` | 2 | 9 | **6** | 1 | 3 | **1** | **5** | ✅ |
| `no-memory` | 1 | 9 | 0 | 4 | 5 | 0 | 0 | ✅ |
| `no-memory` | 2 | 10 | 5 | 4 | 5 | **0** | **0** | ✅ |
| `no-distill` | 1 | 5 | 0 | — | **0** | 0 | 0 | ✅ |
| `no-distill` | 2 | 13 | 0 | — | **0** | 0 | 0 | ✅ |

Three readings, and the first is the one a table like this invites you to skip:

1. **The pass-rate delta is zero.** Six of six passed. On a task this size the run is not
   near the edge, so memory has nothing to rescue — recorded as a zero rather than dropped,
   because "no difference" and "the arms were not wired" look identical from a rate alone
   and the second is what the previous state actually was.
2. **The mechanism fired, and the arms differ exactly as they claim.** `full/rep2` began
   with 6 records, recalled once and put **5 lines** into its prompt; `no-memory/rep2` had
   5 records in its store and served **0**, because `memory=None` is a refusal and not a
   silence; `no-distill` never wrote one, so both its reps were cold by construction.
3. **The counts are not a quality claim.** `no-distill/rep1` took 5 calls and `rep2` 13,
   `full` went 14 → 9: at n=2 that is one run's variance, the same reading `evals/README.md`
   already gives the `out-of-scope`/`tight-window` cells.

⚠️ **One instrument lesson, and it cost a whole sweep.** The first version appended the
scripted "nothing" answer (`[]`) to the end of `H.script_turns(task)`. Those scripts carry
several repair attempts, a scripted run consumes two or three, and the turns left over sit
**in front of** the appended one — so the extraction prompt was answered with the agent's
spare prose and every scripted row reported `distilled=0 failed='schema'`. That reads as a
distillation defect; it was a queue. The extraction is a separate prompt and now gets a
separate script (`run.py::_script_the_extraction`, with
`tests/evals/test_the_memory_arms_can_learn.py::test_the_extraction_gets_its_own_answer` as
the regression). **A scripted answer is only the next answer if nothing else is queued
ahead of it.**

### `two-repos`: the unit of delivery is a repository, not a ticket (M13 Phase E)

The set's other nine tasks all run against one repository, and every one of them would
pass against a runner that delivered to only the first of several. This task is the
falsifier for that: **one ticket naming two repositories, and the row is a pass only if
*each* one was delivered on its own** — its own tree, branch, commit and pull request, its
own checks run in its own tree.

Four pieces make it possible, and only the first is about this task:

- **`EvalTask.extra_repos`** — `{slug, fixture, variable}`, with each `fixture` resolved
  relative to the task directory exactly as the primary one is. `prepare_repos()` builds the
  extra checkout as a **sibling** of the primary (`…/eval-scratch/run-*/repo` and
  `…/reporting`), never inside it: a checkout inside the primary is a file the primary
  repository owns — visible to `git status`, liable to land in the primary's diff, and
  readable by an agent whose brief says it cannot see the sibling. `export_environment(…,
  extras=)` then sets `FORGEPILOT_EXAMPLE_REPO_2` beside the three existing variables.
- **`expect.deliveries`** (an int, or a list of slugs that must each have been attempted)
  and **`expect.delivery_states`**, compared against the row's own per-repository records
  rather than against the plan — a plan naming two repositories and a run delivering one is
  exactly the state this task exists to catch, and comparing the plan against itself would
  miss it.
- **A script with four steps for two deliveries.** ⚠️ One delivery's single attempt is a
  whole turn, and the scripted model has to *finish* that turn: `write A` / `say …` /
  `write B` / `say …`. A two-write script leaves the first delivery waiting for a text turn
  that never comes, which reads as a hang or a budget failure rather than as the script
  being one entry short. `script_turns` pads four more `say "nothing further"` entries for
  the repair path, so the padding is what makes the omission look like anything but itself.
- **`evals/report.py`'s `deliv` column** — the median number of deliveries per row, and `-`
  for a row written before the key existed. **Absent is not zero**: a pre-M13 row has no
  `deliveries` key at all, while a row that recorded an empty tuple planned no repository,
  and printing `0` for the first would read as "this task delivered nothing" — the same
  distinction `_pass_at` draws between a missing number and a failure.

Measured, offline, 2026-10-12: `two-repos` → `pass ready 2 calls 2.5s`, the row recording
`deliveries=2`, `turns=2`, `changed=stats.py,reporting/windows.py`, and two deliveries —
`git.local/demo/stats ready branch=forgepilot/session-…/git.local-demo-stats
changed=stats.py checks=True` and `git.local/demo/reporting ready
branch=…/git.local-demo-reporting changed=reporting/windows.py checks=True`. Two trees, two
branches, each check run where its file lives. The whole offline set is then **10/10**
(`evals/results/20261012T134035-scripted.jsonl`).

⚠️ **The task's own second repository is self-contained, and that is a finding about the
design, not a shortcut.** The plan (D8) said "a small package that imports
`stats.moving_average`", and that cannot run: each delivery's checks run in its own tree and
there is no merged checkout, so an `import stats` fails with `ImportError` for a reason that
is not the bug. `reporting` therefore carries its **own copy** of the same off-by-one — which
is also what makes two independent deliveries the honest shape, because the defect really is
in both places. The gap this leaves is written down in `docs/notes/task-delivery.md`: an
integration suite that needs both repositories in one directory has no home yet.

## Where a search looks, and why that is not where a repository lives

`code_search` passes the session's **working tree** to the retriever
(`SearchQuery.root`), and the retriever prefers it over the path the repository
was configured at. Two questions, and answering one with the other is a silent
wrong answer:

- **Where does this repository live?** — the configured `roots` entry, and the
  place a *mirror* is kept.
- **What is this session editing?** — a checkout cut from it, which after the
  first edit is different content.

Found by running it, and the failure was total rather than subtle. The GitHub
example pointed `retriever.roots` at the mirror, the mirror was **bare**, and
`ripgrep` over a bare repository returns nothing — which `code_search` renders as
*"no match for 'moving_average'"*, **indistinguishable from "that symbol does not
exist"**. The model lost its main discovery tool, fell back to the shell, and
spent 11 of 18 tool calls improvising — including `cd /d C:\…\PycharmProjects\forge
&& dir /s /b`, which left the workspace entirely and found *a different copy of
the same file* in this repository. `read_file` refused (outside the worktree,
the guard working); `run` did not, because there is no containment.

Rules worth keeping:

- **The grant is still checked against `repo`, in the provider.** An override
  changes which *copy* is read, never which repositories may be read. A `root`
  that bypassed the check would make pointing a path a way past the permission.
- **A `root` that is not a directory raises; it never falls back to the
  configured checkout.** A fallback would answer a question about the working
  tree with content from somewhere else — which is the bug.
- **The mirror is a checkout, not a bare repository.** Bare was conventional and
  wrong for the same reason: its path is what a retriever gets pointed at. A bare
  mirror from the old layout is **replaced** rather than reused — it is still a
  valid git repository, `fetch` against it succeeds, and every search against it
  would go on returning nothing while looking healthy.
- **`_has_mirror` is one method because a bare-repo check was written in two
  places.** When the mirror stopped being bare, `ensure_mirror` was updated and
  `push` was not — so `push` refused with "`ensure_mirror` must run first" while
  the mirror sat right there. The same "a correction made at one call site is a
  correction that has not been made" shape, caught by the tests in a minute and
  worth recording because it keeps happening.

⚠️ **The test rig was complicit.** `tests/conftest.py` configured the retriever's
root and the pipeline's workspace as **the same directory**, so "search the
configured path" and "search the working tree" were the same instruction and no
test in the suite could tell them apart. They are now deliberately different, and
reverting the tool's `root=` argument turns 9 of the 12 `code_search` tests red.

**`Retriever.open()` used to read the configured root**, so a slice opened from a
working-tree hit came from the repository's own copy — the same path, different
content, and an answer that looks exactly like an answer. It was left as a
documented gap here because threading the root through it means carrying it on
`CodeRef`, which is a value-object change deserving its own decision. **M14 made
that decision**: `CodeRef` gained `root: str | None = None`, `grep.py::_hit` fills
it in from the root the search actually read, and `open` hands it back to `_root`
— which already refused a working tree that is not there instead of falling back,
so "the same copy the search saw" is structural here rather than a convention. An
index-backed provider leaves `root` as `None`, which is the honest answer for
something with no working tree, and still reads the configured checkout.

⚠️ **Falsified rather than asserted.** Putting `open` back to
`self._root(location.repo, constraints)` turns
`tests/providers/test_grep_retriever.py::test_open_reads_the_copy_the_search_saw`
red — `AssertionError: assert 'edited in this session' in 'MAX_ATTEMPTS = 3\n'`,
the slice silently reading the configured checkout while every inspection says it
read the hit.

✅ **The residual above was closed at M19, and not by touching `open` again.** `read_code`
(`runtime/tools/builtin.py`, `build_search_definitions`) is the consumer: it resolves a
repository path at a revision, calls `Retriever.open`, and reports the revision that was
*served* rather than the one it asked for. Wiring it found two defects — `code_search`'s
renderer dropped the repository and the ref (so a hit from an index could not be turned
into the call `read_code` accepts), and the index provider's `open` matched the ref exactly
while its `search` followed `ref_to_serve` (so the default `<worktree>` request was told
"the index has no such file" about a file it held). The lesson the `ensure_mirror` note
above states still stands, and it is why this took a tool rather than a test: a seam method
nothing reaches is a seam method nobody has exercised against a real caller, and the caller
is what found both.

