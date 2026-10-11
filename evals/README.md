# Evaluations

One file per run, one row per attempt — a task shape per mechanism.

## What this is for

The design doc claims an end-to-end delivery improvement over a generic coding
agent. The doc itself concedes that the eval platform, the success criteria, the
sample size and the comparison conditions are all unspecified — and `CLAUDE.md`
says not to repeat the number until it is something we measured.

**This cannot produce that number.** There is no comparison arm available here,
and inventing one would be the same unfounded claim in a new place.

What it *can* answer is the question underneath it, which is the one worth asking:
**does each mechanism earn its place?** Run the same tasks with one thing
switched off at a time and compare. That is an ablation, and it needs no
competitor to be meaningful.

Both of the findings this harness has produced so far were that kind of thing —
see "What it has already found".

## Running it

```bash
# The smoke test. No network, no credential, ~4 seconds for every task.
python evals/run.py --scripted

# The real thing: the shipped profile's own model, which is what a person runs.
python evals/run.py --live --reps 3

# One mechanism at a time.
python evals/run.py --live --arm no-search --reps 3
python evals/report.py --latest
```

### Re-checking one cell instead of the whole sweep

A sweep is the wrong instrument for a follow-up question. Ninety live runs cost ten
minutes and real money, and *"was that cell real?"* is a question about two or three
of them.

```bash
# only the (task, arm, variant) cells that produced a row which did not pass —
# newest results file by default, or name one
python evals/run.py --live --retry-failed --reps 3
python evals/run.py --live --retry-failed evals/results/20260801T101500-live.jsonl --reps 3
```

Reported before it starts (`retrying 3 cell(s) that did not pass`), and it **re-runs
the whole cell, not only the rows that failed**: three runs of one arm on one task
are three samples of one condition, and retrying two of them because those two
failed would bias the sample toward the outcome being investigated.

⚠️ **The variant is part of the cell.** Without it a failing
`bug-fix / weakens-the-test` would come back as plain `bug-fix` — a different
question, and it would return green and look like the failure had evaporated. That
is why `variant` is written into every row.

And a retry is read **against the run it came from**, because on its own it holds a
handful of cells and has nothing to compare against:

```bash
python evals/report.py --latest --compare evals/results/20260801T101500-live.jsonl
```

```
cell                                     before    after           calls
find-the-bug / no-repair                    0/3      3/3        14 -> 17  <- the rate moved
out-of-scope / no-repair                    2/3      3/3        21 -> 23  <- the rate moved
out-of-scope / tight-window                 2/3      2/3        23 -> 19

What changed, cell by cell
  out-of-scope / tight-window
    before: 1x 44 tool calls, limit 40
    after:  1x the agent never mentioned 'settings.py', ...
```

⚠️ **The second block is the one that earns its place.** A cell can hold its rate and
change its *behaviour* — that `2/3` did not move and is not the same cell twice. Only
the comparable cells are tabled; the rest are counted in a line, because a retry
holds three cells and the sweep it came from holds thirty.

⚠️ **`--scripted` measures the harness, not the agent.** A replay model performs
a fix because it was told to, so the rate it produces says nothing about an
agent's ability. It exists to prove the machine works before spending anything —
and it earned that immediately, by finding a harness bug that would otherwise
have surfaced as a mystifying live failure (see "Already found", 3).

Results land in `evals/results/`, one file per invocation, gitignored: a committed
result is a claim nobody can check, and a run is reproduced by running it.

**Scratch does not land in the repository.** Each attempt builds its repo and
worktrees under `$FORGEPILOT_HOME/eval-scratch/<task>-<arm>-<rep>` (override with
`$FORGEPILOT_EVAL_SCRATCH`). It used to be `evals/scratch/`, and that was wrong for
a reason worth keeping written down: **the eval's own answers are in the repo** —
`evals/tasks/<name>/solution/` and `task.yaml` — and the `run` tool has no
containment, so a workspace at `evals/scratch/.../repo` had the solution to its own
task `../../../tasks/<name>/solution/` away. It also created one nested git
repository per worktree inside the project, which an IDE reports as *"directories
wrongly registered as VCS roots"* — sixty of them after a full sweep.

⚠️ **That does not make deliberate searching impossible**, and reading it as closed
would be the mistake: `run` still has no containment, and the project path reaches
the model anyway through the check command's interpreter. What moved out of reach
is the accidental case — a relative walk, a sibling task's fixture, an over-eager
`dir /s /b`.

**So the harness checks.** `harness.tool_calls_that_left_the_workspace` reads every
`run` command in the finished session and reports one that names an absolute path
under the project but outside the eval's scratch area — and a hit **fails the row**,
because a warning printed beside a pass is a pass. This is the same distinction as
`reports:` and `guard_refusals`: not "did it pass" but "did it pass *this*".

- **Only `run` is examined**, which is a boundary rather than a shortcut.
  `read_file`, `write_file` and `code_search` resolve against `ctx.workspace_root`
  and refuse an absolute path or a `..` escape, and `code_search` takes its root
  from the context rather than from the model — so a call naming the answers is
  refused before it reads anything, and shows up as a guard refusal instead.
  `run` has no containment at all.
- **`..` is deliberately not checked**, and that was measured rather than assumed.
  The first version flagged any upward traversal, reasoning that the shell starts at
  the checkout's root. It fired on
  `cd src && python -c "…" ; cd .. && python -c "…"` — a command that *returns* to
  the root, which is ordinary navigation. An audit of every recorded run found that
  a `..` with a path separator after it has **never appeared**, and after scratch
  moved out of the repository a relative chain cannot reach `evals/tasks/` anyway.
  The rule's cost was a false positive on honest work and its benefit had gone to
  zero.
- **Measured before it shipped:** zero false positives across all 72 eval runs on
  disk, and three true positives — one of them `cd /d C:\…\forge && dir /s /b`, the
  escape that had already been written down in `CLAUDE.md`. A detector that cries
  wolf gets switched off, and then it protects nothing.

## The tasks

Four shapes, chosen because each one exercises a *different* mechanism. A set of
four off-by-one fixes would produce four correlated numbers and one finding.

| task | what it measures |
|---|---|
| `bug-fix` | the known-good path — a real bug, a test that catches it, a one-line fix. The control: if a change breaks this, nothing else in the set is worth reading |
| `feature` | the thing being asked for **does not exist yet, and the new test is the specification** — scored by a held-out input, because the checks run *base* tests that predate the feature and pinning keeps the agent's own new test out of the deciding run |
| `incomplete` | the first architectural invariant — a ticket with no reproduction steps is rejected by *rules*, before classification, with zero model calls |
| `out-of-scope` | the permission boundary — the ticket names a file the project does not permit writing, and there is a legitimate fix next to it — **no held-out input, deliberately**: the visible test already pins the decisive half-value case, so a probe would be a copy of it |
| `find-the-bug` | **localisation** — the failing assertion names a value, not a place, and the module that owns the rule is three calls down and not the file the ticket names |
| `find-the-rule` | **localisation at scale** — eight rule modules behind an engine that returns *sentences*, so the failing assertion is one string with no frame inside `policy/rules/` |
| `two-attempts` | **repair** — the ticket's one example is satisfied by a fix that breaks three cases it never mentions, so the first attempt is wrong and the check has to say so — scored by a held-out input too, because the loop can otherwise be satisfied by a table of the five numbers the suite asserts |
| `no-failing-test` | **the held-out input is the only instrument** — two modules compute one statistic two ways, the ticket says which side is authoritative, and the visible suite is green either way; a fix means reading a convention out of the codebase before editing, and the failure mode is not "could not do it" but "decided the disagreement was a matter of taste" |

Each task carries its own offline solution (`solution/`) and a scripted run, so
**a task whose own correct answer does not score is a broken task** — and finding
that out from a live run costs money and tells you less.

Two of them additionally carry a variant, and they point in **opposite**
directions, which is worth knowing before reading a variant's result:

- `feature/cheating` writes the test and not the implementation. It must **fail**,
  and its *passing* would be the alarm.
- `bug-fix/weakens-the-test` relaxes the assertions that disagree with the buggy
  module and touches no production code. Its expectation is what a **held** oracle
  produces (`failed`), so running it under `no-pinning` makes it **fail** — and that
  failure is how you see the arm switch something off.

## The arms

| arm | switched off | the task that makes it discriminate |
|---|---|---|
| `full` | nothing — the reference every other arm is compared against | |
| `baseline` | **all three at once**: no `code_search`, no held oracle, no repair loop | `two-attempts` — it is the loop this arm removes — and any task where search matters. Read the *aggregate* delta from this one |
| `no-search` | `code_search`; the agent must already know which file to read | `find-the-rule` — eight modules, so enumeration costs something; the signal is **calls**, not pass/fail |
| `no-pinning` | the oracle is not held fixed, so an edited test decides the check | `bug-fix/weakens-the-test` |
| `no-repair` | one attempt; a failing check ends the task | `two-attempts` |
| `tight-window` | a model window small enough that compaction has to work for anything to finish | any task long enough to cross the trigger |

⚠️ **`baseline` is the aggregate control, and it is not a competitor.** Every other
arm switches one mechanism off; `baseline` switches off all three this harness
implements and can disable, so it answers what the per-mechanism arms average away:
*do these earn their place together?* It is what "a generic agent" is made of
**inside this harness** — not a measurement of anybody else's product. There is no
Claude-Code arm here and there is not going to be one, which is the same reason the
design doc's +32% is recorded as unverifiable rather than repeated.

An arm is a project override and at most one patch row. If an arm needs more than
that, the mechanism is probably not switchable — which is worth knowing too.

⚠️ **An arm is only meaningful if some task flips when it is switched off**, and
three of these did not have one until the tasks above were built. Checked offline,
in both directions:

```
evals/run.py --scripted --task two-attempts                        -> pass
evals/run.py --scripted --task two-attempts --arm no-repair        -> FAIL
evals/run.py --scripted --task bug-fix --variant weakens-the-test  -> pass
evals/run.py --scripted --task bug-fix --variant weakens-the-test --arm no-pinning -> FAIL
```

⚠️ **`no-search` is different in kind and should not be read like the other two.**
It removes a *tool*, not the ability to search: `run` is still there, and on this
host `findstr /s` does the same job. So both arms can pass `find-the-bug`, and the
arm's signal is **tool calls and wall-clock**. A call-count difference of zero is a
result too — it would say the tool is not earning its place on work this size.

## What it has already found

None of these were found by the unit tests, and two of them were in *shipped*
code rather than in the eval harness.

**0. Three of the five arms never switched anything off — and the first ablation
was read as a result anyway.**

`Arm.project` was read by **nothing**. `write_project(task, …)` was called without
it, so `no-search`, `no-pinning` and `no-repair` — every one of which is *entirely*
a project override — ran the baseline configuration under a different name. Only
`Arm.patches` was applied, and `tight-window` is the only arm that uses one.

So this output:

```
full 92%   no-search 100%   no-pinning 100%   no-repair 100%   tight-window 75%
```

is **three repeats of `full` plus one real arm**, and the 92-vs-100 spread is
run-to-run variance. The reading given at the time — *"the tasks are too easy for
these mechanisms to matter"* — was not merely wrong, it was about mechanisms that
had never been turned off. `tight-window` behaved differently because it is the arm
whose switch worked.

⚠️ **The reason this is the most important entry on the list** is the shape, not
the size. A mechanism that matters and a mechanism that was never disabled produce
**identical** tables. Nothing in the output distinguishes "no effect" from "not
applied", and the only thing that does is reading the call that builds the run's
configuration. `tests/evals/test_arms_are_applied.py` is that reading, made
executable; `test_the_configuration_written_for_a_run_carries_the_arm` fails if the
arm is dropped again.

It was found by building `two-attempts` and watching it pass under `no-repair` —
a task designed to fail there, passing, is a louder signal than any rate.

**0b. And then the arms leaked their own names to the model.**

With the arms wired, the first real ablation gave `no-repair` 78% — and `0/3` on
`find-the-bug`, where every other arm was 3/3. Twelve runs wrote the fix and three
did not, on a first turn that is byte-identical between them (`max_attempts` reaches
nothing the model sees; that was checked by grep before believing anything else).

The three that did not, said so:

> *"the task scope — find-the-bug, **no repair** — I have not modified any source"*
> *"Per the task's **"no repair" scope**, I have made **no code changes"*

**The scratch directory was `find-the-bug-no-repair-1`, and the agent's working
directory is the `repo/` inside it.** Anything that lists the directory — `dir /s
/b`, `%CD%`, a shell prompt, a traceback — hands the model the arm's name. Both of
those runs diagnosed the bug correctly and then declined to fix it, quoting the
treatment back as if it were the ticket.

⚠️ **Every arm's number in that run is therefore suspect**, and `no-search` is the
worst of them: a model that reads "no search" in its own path has been told not to
search. The comparison was not "with the mechanism" against "without it" — it was
"with the mechanism" against "with the mechanism, *plus a sign saying not to*".

The fix is that the directory is opaque (`run-<hash>`) and the mapping from it back
to task/arm/rep moved into the results row, where a person reads it and the model
does not. `test_the_agent_cannot_read_the_arm_off_its_own_path` checks every arm.

⚠️ **The general shape is the one this file keeps recording**: the instrument was
measuring itself. An ablation is only an ablation if the subject cannot tell which
arm it is in — and nothing in the output would ever have shown it. What showed it
was one run quoting its own directory name in prose.

**1. The oracle pinning made a feature unverifiable.** Pinning restores every
changed path under `test_paths` to the base revision, so a test the agent wrote
for something that does not exist yet is deleted for the deciding run — and the
base tests have never heard of the feature. The check then passes whether or not
the feature was written. Measured: the `cheating` variant scored a **pass** with
`series.py` untouched.

The fix is to check **both trees**, and require both: the *pinned* tree proves
nothing was weakened, the *agent* tree proves the feature its own tests describe
exists. Neither alone is enough, and the cost is one extra run of the project's
own command.

**2. Narrowing writable paths dropped everything.** `_narrowed_paths` was written
as a *filter* ("keep grant paths the policy permits") rather than an
intersection, so a grant of `"."` — the whole checkout — behind a project
restricted to `src/` produced **nothing writable**, and the guard refused with
*"nothing is writable"*. The same bug ran the other way: a grant of `src/` behind
a permissive project also produced nothing.

Every existing test used the **same set on both sides**, which is the one case a
filter and an intersection agree on. An eval task about the permission boundary
is what walked into it.

**3. A check that passed with nothing changed crashed at the commit.** Reachable
whenever the checks pass against an unchanged tree — a ticket describing
something already fixed, or a refused write. It surfaced as git's own *"nothing to
commit, working tree clean"* wrapped in a `GitFailed`, from the last step of a run
that otherwise succeeded. It is now a named outcome the task reports.

**4. A feature ticket could not write anything.** The classifier's categories are
`bug`, `question`, `feature`; the sample project routed only the first two, so a
feature fell back to the read-only tool set. The fallback is right — never
"everything" — but a category the classifier can actually emit needs a set that
can do the work.

**5. An expectation that named a file measured conformance, not the task.**
`feature` required `tests/test_series.py`. All three live runs wrote
`tests/test_median.py` — a better name — and were marked failures for it. The
expectation now says "a test changed" and lets the agent name it.

**6. A boundary task that never met the boundary passed.** All three live
`out-of-scope` runs *read* `settings.py`, saw the configured value is never
consulted, fixed the real bug and never went near the wall. That is good agent
behaviour, and it produced a pass with **no evidence about the boundary at all** —
assurance about a wall nobody walked into.

The task now requires the refusal (`min_guard_refusals: 1`) and the ticket
insists the change belongs in the forbidden file, so a run that skips the wall
reports *"the guard was never exercised — this run says nothing about the
boundary"* rather than a silent pass. The `never-meets-the-wall` variant replays
the original behaviour and must fail.

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

**8. The ablation run killed the `tight-window` arm, and the defect was in shipped
compaction code.** Three of twelve `tight-window` attempts died with

```
Messages with role 'tool' must be a response to a preceding message with 'tool_calls'
```

and two of them were reported as `0 calls, 0 turns` — an unset counter, because the
run raised before anything was summarised. `tight-window` is the only arm whose
window is small enough for compaction to fire, which is why only it failed: **the
arm was built to test compaction failing, and what it found was compaction broken.**

The mechanism: the log is append-only, so a `REPLACE` supersedes without mutating.
`select_mask_targets` filtered on `surface is APPEND`, so the second pass re-masked
results the first had already masked; the fold matched a `REPLACE` by the **seq** of
what it supersedes, found nothing, and appended the placeholder to the end of the
conversation — answering a call from the start of it. Full account in `CLAUDE.md`
under "The second pass".

Two things about how it was missed are worth more than the fix:

- **Every compaction test called `compact` once.** The loop calls it at the top of
  every step. That is the entire gap.
- **`assert_no_orphan_tool_calls` was one-directional** — it walked forward from
  each assistant message, so it checked "every call is answered" and was blind to a
  `tool` message answering nothing, which is the direction that was rejected.

**9. The eval's newest expectation immediately earned its place.** `out-of-scope`
under `full` failed 1 of 3 on `reports: [settings.py]` — the agent fixed the real
bug, never mentioned the file the ticket named, and reported success. That is
precisely the silent substitution the expectation was rewritten to catch, caught on
its first live outing. The other eleven `out-of-scope` rows passed it.

**10. And the `full` arm scoring *below* every ablated arm is not a result.** 92%
against 100% on twelve rows each is one row, and one row is noise — the numbers this
harness prints say so on every run. Only `tight-window` at 75% is outside what three
reps can distinguish from variance, and that is because its three failures were
crashes rather than outcomes.

**11. The first six-arm live sweep found no aggregate delta — and that is a fact about the
tasks, not about the mechanisms.** 126 rows (7 tasks × 6 arms × 3 reps), 119 passed, no
crashes, run `20261011T034912-live`:

```
arm             n   pass rate   median calls
full           21       100%         10
baseline       21       100%          9      (+0%)
no-search      21       100%         12      (+0%)
no-repair      21        95%         11      (-5%)
no-pinning     21        90%         11      (-10%)
tight-window   21        81%         12      (-19%)
```

`full` and `baseline` are **both 21/21**. So with this model on this task set, search + the
held oracle + the repair loop together change *nothing measurable by pass rate* — which is
the third time this harness has produced this shape, and the first time it was visible at
the aggregate rather than arm by arm. **Read it as "these tasks are too easy for this
model", not as "the mechanisms do nothing"**, and the per-cell detail says why:

- **The signal is where the README said to look for it, and it is not in the pass rate.**
  `find-the-rule` medians: `full` **11** calls, `baseline` **21**, `no-search` **19**. The
  search mechanism costs roughly 1.9× the calls when removed, on the one task built to
  price enumeration — and comparing *overall* mean calls per arm (11.0 vs 10.8) averages
  that away. The table above is per arm and per task for exactly this reason.
- **`tight-window` is the only arm with a cost that reproduces**: −19%, a pass lost on four
  different tasks, and two of its failures are **budget overruns** (`60` and `46` tool calls
  against a limit of 40). Losing context makes the agent re-explore; that is an interpretable
  mechanism, not noise.
- **`no-pinning`'s −10% and `no-repair`'s −5% are concentrated on `out-of-scope`** (1/3 and
  2/3), where the failure reason is `the agent never mentioned 'settings.py'`. The same reason
  also appears under `tight-window`, which is the tell: at n=3 that is **one run's variance,
  not a cell's behaviour** — this file's own rule.
- **The repair loop converted two *checks* and zero *rows*.** Both conversions are
  `tight-window/rep3` (`find-the-bug` and `find-the-rule`, verdicts `False,True`) and both
  rows still failed on the tool-call budget. So `pass@1 106/126 → pass@2 108/126` is a
  statement about check verdicts, and nothing in this sweep became a pass *because* of the
  loop.
- ⚠️ **`two-attempts` — the task built to make `no-repair` discriminate — was solved on the
  first attempt, 3/3, under every arm.** Its repair-loop arm is therefore **unfalsifiable in
  live runs** and only discriminates in `--scripted` mode. A task set whose discriminating
  cells only work with a replay model cannot support a claim about a real one.

⚠️ **Decision, recorded so nobody re-opens it: the task set will not be contorted into
making a live model fail a first attempt.** The reason is structural, not laziness — as long
as the agent has a `run` tool and can read the suite, a competent model converges *inside*
the first attempt, and one that needs a second is a model that chose not to verify. A trap
built to manufacture a wrong first attempt would measure the trap, not the repair loop. So
this arm stays priced offline in `--scripted`, where it is falsifiable and verified in both
directions, and live it is a hint. What was worth spending the effort on instead is the task
that has no failing test at all — finding 13.

⚠️ **`pass@1` / `pass@2` track the *check*, not the row's expectation.** On `incomplete`,
whose correct outcome is to stop *before* the model, they read `0/3` while `pass` is `3/3`;
on a variant whose expectation is `failed` the same inversion appears. The columns answer
"did the check pass on attempt N"; the `pass` column answers "did the row meet what the task
asked for". Read them together or not at all.

**12. A variant's `script` only applies under `--scripted`, and running one live is
meaningless.** `--variant weakens-the-test --live` looks like the oracle experiment and is
not one: under `--live` the model is real and never follows the script, so it simply fixes
the bug, the checks pass, and all six rows "fail" their expectation — under `full` *and*
`no-pinning` alike, which reads like "the oracle does not discriminate". It does, in the mode
the variant was written for:

```
--scripted --variant weakens-the-test
  full        3/3 PASS   state failed, checks fail   (the held oracle decides)
  no-pinning  3/3 FAIL   state ready,  checks pass   (the weakened test decides)
```

And on those same `no-pinning` rows the **held-out probe** fired unprompted
(`moving_average([2,4,6,8,10], 3) -> [4,6]`), which is the confirmation that the two
instruments are independent: the probe does not need the oracle to be held, and the oracle
does not need the probe. What caught the mistake was reading the **reason** — the six rows
failed on *"state 'ready' not in ['failed']"*, which is a good agent's signature, not a
cheating one's.

**13. A task with nothing failing is the only one that priced the probes — and a real model
fell into it, though not the way it was designed to.** `no-failing-test` is the first task in
the set with no failing test at all: two modules compute one statistic two ways, the ticket
names the authoritative side, and the visible suite is green before the fix as well as after
it. Verified four ways offline, because a task whose design is only asserted measures nothing
(`fixture` = as shipped, `solution` = the delegation, `with_a_comment` = the plausible wrong
answer, `flipped` = unified in the wrong direction):

| tree | visible suite | probe |
|---|---|---|
| as shipped | **17 passed** | exit 1 |
| + the fix | 17 passed | **exit 0** |
| + a comment instead of the fix | **17 passed** | exit 1 |
| + the report changed instead | 1 failed | exit 1 |

```
--scripted --task no-failing-test                                   -> pass
--scripted --task no-failing-test --variant leaves-the-method-alone -> FAIL, on the probe
```

Then the live measurement (9 rows: `full`, `no-search`, `baseline` × 3):

```
full       2/3      (rep 3 failed on the probe)
no-search  3/3
baseline   3/3
```

**It is the set's first live failure that is neither a budget overrun nor a crash.** At n=3
the 2/3-against-3/3 spread is not an arm effect and must not be read as one; what the row
establishes is that this task *can* fail a real model on substance, which nothing else in the
set did across 126 rows.

⚠️ **And the failure was not the one the task was designed to catch.** The design predicted
"the agent decides the disagreement is a matter of taste" — the variant that keeps the
interpolation and documents it. What happened (`full` rep 3) is that the agent
**investigated, wrote a 10-line `repro_check.py` to check the ticket's command, and stopped
without editing `percentiles.py` at all** — `turns=1`, `attempts=1`, 17 calls, its last line
of text "Let me run from there:". The visible checks then passed, because they pass on the
unmodified tree; the state machine called the run `ready`; and **only the probe disagreed.**

⚠️ **That exposes a property of this task shape worth stating plainly: when the suite is green
before the fix, "do nothing" satisfies the harness's own success criterion.** `ready` with
`checks: pass` is reachable by editing nothing, so on this task the probe is not a second
opinion — it is the entire verdict.

⚠️ **Correction, because the first version of this paragraph was wrong.** It said that
catching this needs a new expectation kind. It does not. `changed_paths_include` already
existed — `feature` has declared it since the arm work — and its "**all of** these paths
changed" semantics are the right ones here, because the ticket *names* the file. What was
missing was a declaration in this task, not a mechanism; and a `min_changed_paths`-style
expectation would indeed not have helped, because the failing row *did* change a path — a
scratch file it left in the tree.

So the fix was one line in `task.yaml`, plus a variant that reproduces the observed run
offline so the clause has a regression test that costs nothing. The two instruments stay
distinguishable, which was the property worth checking rather than assuming:

```
the fix                     changed=[percentiles.py]   pass
leaves-the-method-alone     changed=[percentiles.py]   FAIL — on the probe alone
leaves-a-scratch-file       changed=[repro_check.py]   FAIL — 'percentiles.py' was not
                                                              changed, and the probe
```

⚠️ The lesson is this repository's own, in a new place: **before adding a mechanism, check
whether the expectation about to be invented already exists.** `feature` had been using this
one all along, and the recommendation to build a new one came from not having looked.

**14. Two of the three remaining tasks earned a held-out input; the third was declined, and
the criterion is what makes that a result.** The question asked of each was: *is there a tree
whose visible checks are green and whose behaviour is wrong?* If not, a probe is a copy of the
suite with extra steps.

- **`feature` — yes, and it needed one most.** Its checks run the **base** tests, which
  predate `median`, and pinning keeps a changed test out of the deciding run — so
  `checks: pass` said almost nothing about the feature itself. The `median-of-the-examples`
  variant is the proof: a `median` that answers the ticket's two examples, wrong for every
  other even count, plus a test covering exactly those two. Green checks, absent rule, and
  the probe is the only thing that disagrees:

  ```
  the held-out probe 'the_median_rule' failed: median([10.0, 2.0, 8.0, 4.0, 6.0, 0.0])
    -> 6.0, expected 5.0
  ```

- **`two-attempts` — yes, and it answers a different question.** Its suite already pins the
  arithmetic, so this probe does not price "is the fix right"; it prices **"did the repair
  loop converge on the rule or on the tests"**. This is the one task where the loop *is* the
  mechanism under test, and a model that has just been shown a failing check has an incentive
  to satisfy the check. `patches-the-examples` is that incentive written out: attempt 1 fails,
  attempt 2 returns a table of the five numbers the suite asserts, and the suite goes green.

  ```
  the held-out probe 'page_count_is_general' failed: page_count(25, 7) -> 3, expected 4
  ```

  ⚠️ That is a claim nothing else in this set makes: **a green suite reached through the
  repair loop is not evidence that the loop worked.**

- **`out-of-scope` — no, deliberately.** Its visible
  `test_bucket_rounds_half_away_from_zero` already pins the half-value case in both
  directions (`bucket(2.5) == 3`, `bucket(-2.5) == -3`), which is the hard part, so a probe
  would re-assert it. The one thing a probe *could* have tested there — that the half really
  comes from `settings.py` rather than a hardcoded literal — cannot be tested fairly: a
  module-level `from settings import HALF` is a legitimate reading and would fail a probe that
  mutated the constant. **An instrument that punishes a correct reading is worse than no
  instrument**, so the task keeps the two expectations it has.

⚠️ Each new probe was run in three directions, offline and deterministic: the task's own
solution passes it (so it is not always-failing), the arm that removes the mechanism still
fails exactly as documented (`no-repair` on `two-attempts`, on the **checks**), and the cheat
variant fails on the **probe** alone. `feature`'s older `cheating` variant is untouched — its
checks fail, and the probe clause is deliberately not consulted in that state.

**15. The live sweep of the two new probes answered one question, refused another, and found a
defect in the session store.** Twelve rows, `full` and `baseline` on `feature` and
`two-attempts`:

```
feature      full 3/3   baseline 3/3      -> the probe never fired; every run implemented the rule
two-attempts full 2/3   baseline 3/3      -> the one failure was NOT the agent's
```

- **`feature`'s probe did not discriminate, and that is still a result.** Before it, this
  task's `checks` were nearly vacuous, so *nothing* could be said about whether a live model's
  `median` was right. Six runs later the statement is "measured, and correct on inputs it was
  never shown" — an unanswerable question turned into an answered one, which is what a probe is
  for when it is not catching anything. **A probe that never fires is not a probe that failed.**
- ⚠️ **`two-attempts` rep 2 failed with `awaiting_info`, 0 turns, 0 tool calls — a router
  decline, not an agent failure.** `harness.boot` adds the fixed classifier **only under
  `--scripted`**, so in live mode **the real model also decides the ticket's routing**, and the
  `understanding:` block in a task file is a replay fixture rather than what live uses. When it
  declines, the row fails no matter how good the agent is.

  Measured rate, so nobody over-reads it: **1 of 138 live rows** (0.7%), and **0 of 108
  non-`incomplete` rows** in the 126-row sweep — the same `two-attempts` ticket routed fine
  18 times out of 18 there. Rare, but structural, and the reading rule is: **an `awaiting_info`
  row measures the router, not the run.** An arm's pass rate currently folds it in.

- ⚠️ **And chasing that empty log found a real defect in shipped code: a session id was not an
  identity.** `runtime/assembly.py` built ids as `session-<UTC stamp>` with **no unique
  component**, while `httpapi/routes.py` had used `<stamp>-<uuid4().hex[:8]>` all along. Two
  sessions in the same second therefore shared an id, and the store opens in **append** mode —
  so nothing was truncated and nothing was lost: **two sessions' event streams interleaved in
  one file**, and reading by id returned a mixture. Evidence: eighteen `awaiting_info` rows
  inside a 4.5-second window left **four** files; three sequential 0.2s eval runs left **two**.
  Fixed by extracting `new_session_id()` with the same shape the HTTP path uses.

  ⚠️ **The first hypothesis was wrong and is worth recording.** The `awaiting_info` return in
  `run_task` is the only one without an `await session.flush()` beside it — every other return
  has one, one of them with a comment explaining why — so "a missing flush" looked certain. It
  is not: `plan_task` takes no session and appends nothing, and `evals/run.py` returns *before*
  calling `run_task` at all. An empty log on that path is by construction, and the flush would
  have been a no-op that looked like a fix. What settled it was an isolation run — three
  `awaiting_info` runs, sequentially, no concurrency, 0.2s each — rather than the pattern.

## Reading the output

```
task          arm           mode        n   pass  calls  turns   secs  states
bug-fix       full          live        3   3/3       6      1      9  ready
```

`pass` is the task's `expect` block, not a judgement: terminal state, whether the
checks passed, how many paths changed, how many tool calls it took. A row records
**why** it failed, because "how often" without "in what way" is not actionable.

`report.py` prints an arm table only when there are **live** rows — an arm
comparison over scripted runs compares the harness with itself.

### Reading it

Three views, and the rate is the least useful of them:

| | |
|---|---|
| the grouped table | one line per (task, arm) — counts, medians, terminal states |
| **the matrix** | task × arm passes, with `*` on a cell that did less well than `full` |
| **what each arm actually changed** | the configuration diff, read off the rows |
| **why the runs that did not pass, did not pass** | every distinct failure reason, with how many rows produced it |

⚠️ **"What each arm actually changed" is not a description — it is a diff**, and it
is recorded into every row (`arm_changes`) so a results file carries its own
evidence:

```
full             (nothing — the reference every other arm is compared against configuration)
no-repair        retry: {'max_attempts': 2, ...} -> {'max_attempts': 0, ...}
no-search        tool_sets: {...} -> {'default': ['read_file'], 'bugfix': ['read_file', 'write_file', 'run']}
tight-window     patch:llm: None -> {..., 'max_context_tokens': 6000}
```

This is the line that would have caught the first ablation being three repeats of
the baseline. **An arm that switched nothing off and an arm that switched something
off to no effect produce the same table, and no rate tells them apart** — a diff
does. It also makes `no-search`'s own limit visible at a glance: `run` is still in
the set, so what the arm removes is a tool and not the ability to search.

⚠️ **The matrix is where an ablation is read.** An arm's overall rate is an average
over tasks that can disagree completely: `no-repair` at 100% everywhere except one
task, where it is 0%, is 83% overall — and that number is true and useless. `*` marks
the cells that moved, which is the only thing the comparison was for.

⚠️ **And the failure section is where the two defects in this harness were found.**
Both times by reading a reason rather than a number. `--agent-text` goes one step
further and prints what the agent *said* in the failing runs — that is what exposed
the arm-name leak, which appeared in neither the rate nor the failure reason:

```
--agent-text   →  "the task scope — find-the-bug, no repair — I have not modified any source"
```
