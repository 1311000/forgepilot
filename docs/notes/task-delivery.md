# Task mode, delivery, the repo host and the decision authority

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before changing `orchestration/modes/task.py`, the workspace/vcs split, pinning, or the publishing step**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## Task mode notes

A mocked ticket now becomes a branch, a real diff and a pull request draft, deterministically, with no network, no git binary and no test runner.

- **Delivery is harness-driven, not model-driven.** There is no `open_pr` tool, and none is registered: opening a pull request is a privileged outward-facing act, and the fixed workflow already decides when it happens — once the checks pass. Every capability that exists is one that can be induced, so the model gets none it does not need.
- **"Once the checks pass" is not the same as "always".** That sentence was true of the *policy* and false of the *code*: `open_pull_request` was called unconditionally, which made a service that owns pull requests a hard requirement for finishing a task. It now depends on `"pull_requests" in vcs.capabilities()`. With the capability, the state is `DELIVERED` and a PR is opened; without it, `TaskState.READY` and the **draft is the deliverable** — checks passed, commit made, body written from the real diff and the real check output. `READY` is deliberately not `DELIVERED` (nothing was published) and not `FAILED` (nothing went wrong).
- **The tool selection is enforced at execution, not just advertised.** Restricting the schemas the model is *shown* is not enough — a model can name a tool it was never offered, and the registry would run it. `ToolPipeline.allowed_tools` is set from the plan's selection, and a call outside it is refused *and recorded*, so the model sees why.
- **The harness's check is its own event kind.** `task/check` is surfaced to the model (it must see why something failed) but is deliberately **not** a `tool/result`: the model never asked for it, and a tool result with no preceding assistant tool call is an orphan message that endpoints reject outright. It renders as a user message. Pinned by `test_a_harness_check_reaches_the_model_as_a_user_message` and by an orphan-check over the whole conversation.
- **A check that cannot run is never a pass.** Missing `RUN_TESTS`, an unexecuting sandbox, or no configured commands all produce `could_not_run` and a failed task — reporting success for checks nobody ran would claim verification that did not happen.
- **The repair loop is bounded by `project.yaml`'s `retry` policy**, and gives up early when a check `could_not_run` rather than looping against a wall.
- **The workspace diff is real**, computed with `difflib` against a snapshot taken at `prepare`. Returning `""` would have made "the ticket produced a diff" an assertion about the mock rather than about what the model changed.
- **`run_task` flushes before returning.** It did not, and a finished task's `task/check` and closing `task/state` events sat in the buffer — a reader, or a crash, saw a task that never ended. Found by a test, not by inspection.

## One repository per task, then N (P5-2 → M13)

`preprocess` returns **every** repository a ticket names, because that is what the
ticket says; when it names none, `plan_task` falls back to the project's own
`repos:` list. Both are tuples. Both were honoured by a plan that then did
`repo = plan.repos[0]` — one worktree, one commit, one pull request, and the rest
discarded **without a word**. A ticket naming two authorised repositories was
fixed in one of them and reported `READY` / `DELIVERED`.

⚠️ **This is the defect shape of this codebase in its most expensive form**, and
for once the cost is not a missing check: the plan *said* two and the execution
did one, so nothing downstream — not the state machine, not the checks, not the
draft — could tell. For a cross-repository change the untouched half is precisely
the part nobody goes to look at, which is why the silent version is worse than a
refusal would have been.

**The first answer was a refusal (P5-2).** `plan_task` sent a ticket naming more than
one target to `AWAITING_INFO`, naming the repositories in the reason so a person could
split the ticket — before the classifier, for the reason an incomplete ticket is: the
answer is a rule, and asking a model which half of a change to make would be asking it
to decide where to write. `run_task` asserted the invariant it relied on
(`len(plan.repos) == 1`), so a two-repository plan failed where it was used rather than
delivering half a change. **That was a policy answer to a capability gap, and M13 built
the capability** (`~/.claude/plans/m13-cross-repo-delivery.md`, decisions D1–D8).

**M13: the unit of delivery is a `(session, repository)` pair.** `run_task` is a serial
`for repo in plan.repos:` loop, and each delivery is a `RepoDelivery` with its own

- **working tree**, the handle now keyed `(session_id, repo.slug)` rather than on the
  session alone (the P5-2-era bug in its purest form: `prepare(repo_b, …)` in one session
  returned **repo A's** tree, and its branch, and reported repo B's changes against A's
  baseline);
- **branch name**, `forgepilot/<session>/<slug>`;
- **brief**, `compose_delivery_brief` in `orchestration/context_assembly.py` — the turn is
  told which repository it is in and, when the ticket spans several, that the siblings'
  files are **not** in this tree ("a change that belongs in one of them cannot be made
  here"). Without that sentence a model told to fix two repositories does the natural
  thing and writes only what it can see;
- **recall scope**, `Recaller(scope_ref=repo.slug)` — what was learned about *this*
  repository, not about its sibling;
- **attempt budget**: `project.retry.max_attempts` is per delivery, not per ticket;
- **commit, push and pull request**; and
- **state machine**. One machine cannot serve two sequential deliveries —
  `ALLOWED_TRANSITIONS` has no edge from `delivered` back to `running` — and using one
  would either crash or need a fake transition. So each delivery runs its own machine and
  the task's machine records only the verdict.

**The verdict is a function of the deliveries**, not a parallel bookkeeping:
`DELIVERED` when every repository was delivered, `READY` when none failed but not all were
published, `FAILED` when any failed. Attempts and turns are summed for the row.

**A failure stops the run, and the stop is recorded rather than assumed.** Repositories
after the failure are reported `not attempted` (`RepoDelivery.attempted` is
`state is not TaskState.QUEUED`), with no tree, branch or commit. Parallel delivery was
refused deliberately: there is one session log, one approval path, and `run_turn`'s
`workspace_root` is a single directory — and the trade-off is paid in public, because
**git has no un-push**: repo A's pull request is already open when repo B is verified. The
cost of that ordering is one reason the loop is serial and not a work queue.

**Each repository's checks run in its own tree** (D3). There is one `project.yaml` `test`
command, and it runs once per repository, there — so "the other repository's suite also
passes" has an answer that can be measured, and `task/check` events carry `repo` because
in one log two repositories' checks interleave and a passing check that does not say which
tree it ran in cannot be told apart from the other one's. The scripted eval run shows the
whole shape: two deliveries, two branches (`…/git.local-demo-stats`,
`…/git.local-demo-reporting`), `stats.py` changed in one and `reporting/windows.py` in the
other, each check run where its file lives.

**Plurality comes from the ticket, never from configuration — and that is an amendment to
D4's neighbourhood, made during Phase B.** The project's `repos:` list is a fallback only
when it declares **exactly one** repository. A silent ticket against a project declaring
several is a third `AWAITING_INFO`, naming them: the alternative ("deliver to all of
them") would let the configuration manufacture N pull requests and would make the split
the PR body explains the *configuration's* assumption rather than the ticket's
description. The reason string says which of the three it is.

**The pull request body explains the split, and says nothing extra when there is none.**
When a ticket covers one repository the body is **byte for byte** what it was before M13
(pinned by `test_a_single_repository_body_is_byte_for_byte_what_it_always_was`); when it
covers several, a section lists each sibling as this pull request / `#number` (already
opened) / "committed on `<branch>` and verified, but not published" / **failed**: reason /
not attempted. The state events carry `repo`, and there is **no aggregate per-task state
event** on purpose: a single-repository log still reads `running → delivered`, which is
what its tests assert. Because publishing is serial, the sibling paragraph is written from
what the run knew *at that moment* and rewritten once every delivery has an outcome (the
`task/published` events; see below) — the opening body of a plural ticket's first pull
request is the one place the run has to say "not yet" and then stop saying it.

**The merged checkout — added at M25, and only for one kind of command.** The gap this
section used to end on ("an integration suite that needs both repositories in one
directory") is closed for the check that needs it and deliberately not for anything
else:

- `ProjectConfig.merged_checks` is a tuple of commands run **after** every delivery, in a
  directory built from the deliveries' working trees. It is a command the *harness* runs
  (like `test:`), not something the model may launch (`commands:` is that allow-list).
- `MergedCheckoutWorkspacePlugin` (in `providers/workspace/merged.py`) **inherits**
  `GitWorktreeWorkspacePlugin` and adds `merged_checkout(trees, session_id=…, name=…)`:
  one directory, one subdirectory per repository named by the last slug component, copies
  of the **working trees** (so uncommitted fixes are what gets checked), no `.git`, and a
  `ValueError` naming both slugs when two repositories end in the same name. It inherits
  rather than wraps because Cordis provides a service at construction: two instances would
  fight over the `workspace` name.
- `run_task` skips it — with a reason, never silently — when there are fewer than two
  attempts, when a delivery failed, when a delivery has no recorded `root`, or when the
  configured workspace cannot build one (the reason names the provider to use). A failing
  merged check turns the task `FAILED` with `"the merged checks failed, so the
  repositories do not work together: …"`, and says whether anything was already published.
  A **skipped** one leaves the per-repository verdict alone and appends `"; the merged
  checks did not run: …"` to the reason, because a check that could not run is not a check
  that passed.
- Measured (2026-10-19): `two-repos` in `evals/` now runs it — 8 arms, all green, 2 calls
  each — and breaking the command turns the task `FAILED` in 2.4 s. The real command is in
  `examples/stat-fix/two-repos/project.yaml`: before the fix both copies drop the last
  window and therefore *agree*, so it compares against a written-out expected value rather
  than the two against each other. The falsifier
  (`tests/task/test_merged_checks.py::test_the_check_catches_a_combination_that_each_tree_passes`)
  runs it for real in a merged directory: passing, then `(3, 2)` fails it.

**Still not covered:** a *per-repository* `test`/`test_paths` (every repository still runs
the project's one command in its own tree), and a merged tree the agent itself may read —
`merged_checks` is verification, not a workspace. Both are deliberate: the second would
hand the agent a tree that no delivery owns, and a diff from it could not be attributed.

**Decided at Q5 (2026-10-20): the project owns one check command, and a project whose
repositories differ dispatches inside it.** A `test:` per repository would need the
harness to know which tree each line belongs to before it runs, and the only source for
that is the configuration — so a two-repository project with two different suites would
be making the *configuration* decide what each delivery's check means, which is the same
mistake M13 refused when it made plurality come from the ticket rather than from
`project.yaml`. The supported shape is one command that can tell where it is (the
existing `run_checks` runs it with `cwd` set to that delivery's tree), and
`examples/stat-fix/two-repos/project.yaml` is the worked example: `pytest -q` in a tree
that carries one copy of the defect. Trigger for revisiting: a real deployment where the
per-repository commands cannot be expressed as one dispatch, *and* a place to put the
mapping that is not guessed from the configuration.

**Two pull requests against a real host — measured 2026-10-18, not asserted.** A single run
of `BUG-2` through `examples/stat-fix/two-repos/github/` (the layout this section used to
say did not exist) delivered both repositories to `github.com/1311000`: draft
[#3](https://github.com/1311000/test/pull/3) changing `stats.py` with the checks at
`8 passed`, and draft [#1](https://github.com/1311000/test-reporting/pull/1) changing
`reporting/windows.py` with `7 passed` — two mirrors, two worktrees, two branches
(`forgepilot/session-<stamp>/<slug>`), one commit and one pull request each, and each pull
request's body naming the sibling.

That run found a defect **the local and mock providers could not show**, and it is the same
shape this repository keeps finding: a sentence that looks like it was computed from the
outcome and was computed from the *run order*. Publishing is serial, so `#3`'s body was
written while `test-reporting` had no verdict yet, and it said "not attempted by this run,
which delivers one repository at a time and stops at the first failure" — about a repository
that was delivered as `#1` seconds later. Nothing rewrote it. The fix is
`Vcs.update_pull_request_body()` plus `_rewrite_sibling_sections()` in
`orchestration/modes/task.py`: once every delivery has an outcome, each body's sibling
paragraph is recomposed from the same recorded values and `PATCH`ed, and each rewrite (or
its failure) is a `task/published` event, because a stale body is a fact about the run and
not something a reader should have to infer. A failed rewrite does **not** fail the task:
the change is in the pull request, the description is stale, and the log says so. The
already-open `#3` was corrected on the host with the same wording by
`.tmp/fix_pr3_body.py` (gitignored), so the artifact and the code now agree.

The same run showed a second, quieter leak in the outward artifact: the `## Checks`
line quoted `D:\…\.venv\Scripts\python.exe -m pytest -q`. The command is expanded from
`${FORGEPILOT_PYTHON}` at `project.yaml` load time because the sandbox's environment is
an allow-list and the check really has to start a concrete interpreter — but *where it
ran* is not what the body is for, and it is read on machines that do not have that path.
`ProjectConfig.commands_declared` keeps the line as the project wrote it,
`_declared_checks()` falls back to the expanded form when a project was built from an
already-expanded mapping, and `_pull_request_body(declared=…)` prefers the declaration.
All four open bodies on the two fixture repositories were corrected in place on
2026-10-18 with the declared line (`${FORGEPILOT_PYTHON} -m pytest -q`), including the
two older drafts from 2026-10-10 that carried the same expanded path.

Two smaller notes. `examples/stat-fix/two-repos/github/` is a *third* deployment directory
rather than a flag on the second, because `ProjectConfig.repos` is what ticket text is
resolved against — mutating slugs at run time would make the text and the project disagree.
And `README.md`'s「跨仓」and the design doc's 300GB-across-repos premise are now
served by the runner as well as by the seam (`ProjectConfig.repos` is a tuple,
`Retriever.search` takes `repos`), but only within one deployment's declared slug list.


## Task entry point notes (`--task`, `examples/stat-fix/`, `providers/tickets/file.py`)

The workflow was complete and **unreachable**. `run_task` had tests, a repair loop and a real diff, and no caller outside `tests/` — the first end-to-end run needed a driver script in a directory that is not this repository. This section is what changed, and what running it exposed.

**Five rows have to be swapped for any of it to work**, and each is a thing the default profile correctly is not. They are the answer to "what is this profile honestly not?", so they are worth knowing as a list:

| Row | Default | What breaks without the swap |
|---|---|---|
| `tickets` | in-memory fixtures, empty | `--task BUG-1` reports "no such ticket" — the mock only knows what a test registered |
| `classifier` | `NullClassifierPlugin`, always declines | a decline is `AWAITING_INFO`, so **every** ticket stops at a human: task mode is unreachable |
| `workspace` | copies files to a temp directory | no repository → no branch, no commit, no diff |
| `retriever` | ripgrep with `roots: {}` | `UnknownCheckout` — a shipped default cannot know a deployment's paths |
| `vcs` | `MockVcsPlugin`, which declares `pull_requests` | a fabricated PR reference, so the task reports `DELIVERED` on a pull request that does not exist |

The last row is the one that matters most: `LocalRepositoryVcsPlugin` is defined mostly by what it *cannot* do, and that is what makes the run end at `READY` rather than lie.

**The ticket is fetched through the seam, never built by the CLI.** A CLI that constructed its own `Ticket` would keep working after a deployment swapped the tracker out, and would be reporting on a ticket nobody filed. `providers/tickets/file.py` exists so there is something real on the other side: `MockTicketSourcePlugin`'s fixtures are *code*, which is right for a test and useless for a batch run.

Two things about it are load-bearing, and both are about failing where someone can see it:

- **Loaded eagerly at boot.** A malformed ticket file is a configuration error; the same reasoning that makes a malformed `FORGEPILOT_DEV_TOKENS` raise applies. Lazy loading surfaces the problem only when that one ticket is fetched, which in a batch run is an hour in.
- **An empty source raises, and an unknown id lists what it has.** "No such ticket" from a directory of forty files leaves the reader guessing whether it was a typo or an empty source — and `()` for "I found nothing" is indistinguishable from "there is nothing", which is the same argument as `UnknownCheckout` in the retriever.

**`project.yaml` now expands `${VAR}` the same way a profile's plugin config does.** It earns its place in *this* file for a reason it does not have in a profile: a `test:` command is a **machine's** business. The interpreter that has pytest differs per developer, and `project.yaml` is a file a team checks in.

Three smaller things, each found by running it:

- **`git` marks its object files read-only, and `shutil.rmtree` on Windows raises `PermissionError` on them** rather than clearing the attribute. Recreating a checkout means `onexc` → `chmod` → retry. The first `prepare_repo.py` died on `.git/objects/...`.
- **`--export` must not destroy.** It exists to be put inside `eval "$(...)"`, which people put in scripts — a command that deletes the checkout every time it is sourced would discard a run in progress and look like it had only printed two variables. `prepare_repo.py` recreates on an interactive run and only creates-if-missing for `--export`.
- **The CLI printed mojibake.** With stdout a pipe, Python encodes with the machine's ANSI code page, so a Chinese ticket title arrived mangled and the warning glyph arrived as its backslash-u escape instead of the character. `_use_utf8_output()` reconfigures both streams with `errors="replace"` — output that cannot be encoded should show a placeholder, not abort a run that has already done the work.

**Reading the run's own session log found the most important defect of the batch**, and it was not visible from the outcome. The task reported `ready`, `check: pass`, a correct one-line diff — and the log showed the model's *own* `run` calls never executed, because the `run` tool still had the `shlex.split(posix=True)` bug that `run_checks` had already been fixed for. Worse, the tool blamed the sandbox for it, so the model concluded execution was impossible and stopped trying. It got the right answer by reading code instead of by testing. **A green outcome is not evidence that the path to it was sound**; the log is where the difference shows.

**The "the agent can edit the tests" gap is closed** — see "The oracle is held fixed" below. It was the largest hole left: `run_checks` passed whether the code was fixed or the assertion was relaxed, and nothing in the harness could tell the two apart.

## The commit step, when the agent already committed (measured 2026-10-18)

`providers/workspace/git_worktree.py::commit` runs once per delivery, after the checks. It stages the whole tree with `git add -A` and asks git whether anything is staged; an empty answer means "no change was produced", so it raised `NothingToCommit` and `run_task` reported `the checks passed but nothing was changed, so there is nothing to deliver`.

That reasoning is sound only if nobody else commits. **`run` is a shell, and `git commit` is a command the agent can type.** Measured live on the second repository of a two-repository task: the same delivery record carried `changed: ["reporting/windows.py"]` and `checks_passed: true` **and** `state: failed`, because the model had run `git add reporting/windows.py && git commit -F _commit_msg.txt` itself. The working tree was clean and the branch was ahead — a true statement about `git status`, a false one about the delivery.

The rule now: nothing staged, but `git diff --name-only <base_ref>..HEAD` is non-empty, means the branch already carries the work. `commit` then returns `HEAD` as the delivery's commit and **does not** create a second one (an empty commit would be a lie about who changed what). If the branch is ahead while the net change is empty — commits that cancel out — `NothingToCommit` still stands, and `test_a_commit_that_changes_nothing_back_is_still_refused` is the guard against broadening "ahead" into "delivered".

Nothing about the boundary moved: a commit the agent typed is subject to the same diff, the same `writable_paths`, the same pinned tests and the same review. What changed is that the platform stopped treating one particular way of producing the change as the absence of one. Reverting the branch-diff check fails `test_a_change_the_agent_committed_itself_is_still_delivered` with the production message.

## TAPD as a ticket source (`providers/tickets/tapd.py`, `bundles/tickets_tapd/`)

The seam was modelled on TAPD's field shapes from M2 and the design doc named
`tickets/tapd.py` as the stub to fill; M22 filled it. Two modes, one parser:

- **`replay`** answers from a *cassette* — the exact JSON payloads the API returned, keyed by
  the request that produced them (`GET /bugs?id=…&workspace_id=…`). It needs no token and no
  network, which is what makes a ticket-backed test reproducible.
- **`live`** asks `api.tapd.cn`, and **`record:` writes the cassette as it goes**. There is no
  separate recorder script on purpose: the writer and the reader are one code path, so a
  recording cannot drift from the reader that replays it. A test records through a
  `MockTransport`, then replays the file with nothing but the file, and asserts both tickets
  are equal.

Four decisions that are about failing where someone can see it:

- **A cassette miss names the requests the file holds.** "No such ticket" and "the recording
  has no such request" are the same answer at the call site, and only one of them is true.
- **TAPD's `status` is a success flag, not the bug status of the same name.** `_checked()` reads
  the envelope's flag; the bug's own `status` is one level down. Conflating them turns a refused
  token into an empty result.
- **Priorities are mapped, never defaulted.** TAPD's are numbers (`1`…`4`); an unmapped value
  raises naming the ticket and the value. A default would make every bug `medium` at once.
- **The token is read from the environment by name** (`FORGEPILOT_TAPD_TOKEN`), like the GitHub
  token and for the same reason: a value in a patch file lands in `git status` and in
  `--dump-config`. Error text never repeats the query string, which carries it.

⚠️ **No live TAPD call was made from this machine — there is no TAPD credential here.** The
payload shapes come from TAPD's documented responses and are exercised through the cassette
round trip, not observed against the real service. That is a real limit of the evidence: a
field named differently upstream would show up as a `TapdRequestFailed` naming the ticket, not
as a silently empty ticket, but it would show up for the first deployment rather than here.
Recording once with a token is the intended way to convert this into observed evidence — and
the `record:` path is exactly that act, not a tool written for it.

`bundles/tickets_tapd/patch.yaml` overrides the `tickets` row by id; `workspace_id` has **no
default** for the same reason `index_path` has none, because a default project would resolve a
ticket id in a project nobody named.

## The oracle is held fixed (`runtime/pinning.py`, `project.yaml: test_paths`)

An agent that cannot make a check pass has a cheaper option: make the check stop testing the thing. `run_checks` could not tell "fixed the code" from "relaxed the assertion" — both are a green run over whatever is in the tree, and the tree is the agent's.

So the project's test paths are **restored to the task's base revision for the duration of the check**, and the agent's versions are put back afterwards. The weakened test then *fails*, which is a signal the repair loop already knows how to act on. A warning would not be: the failure this guards against is the one that looks fine.

What it deliberately does not do:

- **It does not stop the agent editing tests.** Adding a regression test is ordinary practice. The edit is kept, shown in the diff, and named in the draft's body under a heading that says it was not what the checks ran against. It simply does not decide the verdict.
- **It does not pin paths the project does not call tests.** A file that is genuinely part of the change has to stay the agent's, or every run would be judged against code without the fix.

Rules worth keeping:

- **`test_paths` defaults to a convention and the default errs toward pinning more.** Pinning a path that turns out not to be a test makes a check run against older code, which fails *loudly*; missing one lets a weakened test decide the run, which does not fail at all. A trailing slash means a directory **at any depth**, matched component-wise — so `tests/` covers `a/tests/x.py` and does not cover `mytests/x.py`. An explicit `[]` means "pin nothing", which is a different statement from omitting the key.
- **The model is told, in the check output.** A repair loop that adjusts an assertion, fails again, and is given no reason will spend its whole budget on the wrong file. The notice is *prepended* — on a failure the tail is the traceback, and a note after it is a note nobody reads.
- **`restore` deletes a path the ref never had.** "The agent added a test file" and "the agent weakened one" have to be undone the same way, or the added file survives and keeps taking part in the run it was excluded from. Deleting a test is the cheapest way to make a suite pass and the one that leaves the least trace in a diff.
- **The `Workspace` seam's `restore` is not reachable by a model and takes no constraints.** It restores *away* the agent's work by definition, which is exactly what makes it dangerous to expose. `LocalTempWorkspacePlugin` refuses it rather than no-op'ing: a silent no-op means the agent's tests decide a run whose log says otherwise.
- **A workspace is never created where the caller happened to stand.** `LocalTempWorkspacePlugin` resolves its root from the row's `base_dir`, then `FORGEPILOT_TMP`, then `tempfile.gettempdir()` — and refuses outright when the last of those is the working directory. That is not a hypothetical: `tempfile`'s documented last resort is `os.getcwd()`, and on a host whose sandbox denies every real temp directory (measured here: `%TEMP%`, `%TMP%`, `C:\Windows\Temp` and `c:\tmp` each raise `PermissionError: [Errno 13] Permission denied` on `mkstemp`, and the repository root was the only creatable candidate) the last resort is what *actually happens*. Fourteen `forgepilot-*` checkouts had accumulated in the project's front door before this refused. It matters more than tidiness: a workspace root is the boundary the sandbox enforces and the tree the retriever searches, so the quiet version of this is a checkout *inside the project* that `git status`, a whole-tree search and `--workspace .` all see.
- **The per-attempt loop re-pins**, because each turn may edit the tests again. The outcome reports the *union* across attempts — a path pinned on the first attempt and reverted by the agent on the second was still kept out of a run whose verdict is in that outcome.

**And a defect this found on its first run.** The pinned set came back containing `tests/__pycache__/test_login.cpython-314-pytest-9.1.1.pyc`. The check command runs *inside the tree it is judging*, so pytest's byte-compiled caches are untracked files, and `changed_paths` — which asks git what is untracked — reported them as the agent's work. `command_env()` now sets `PYTHONDONTWRITEBYTECODE=1`, forced rather than inherited. The general shape is worth keeping: **a checker must not modify the thing it is checking.** A build step that writes real artefacts into the checkout is the same problem and is *not* covered; git's answer there is a `.gitignore`, and a repository without one will see its build output in its own diff.


## The repo host (`providers/vcs/github.py`, `examples/stat-fix/github/`)

The provider that makes `DELIVERED` reachable. It is the only implementation of
`push` and `pull_requests`, which were the two capabilities every other provider
honestly declined, and it is what turns "the draft is ready" into a branch on a
host and a pull request a reviewer can open.

**⚠️ Vocabulary.** The *host* is the machine a command runs on. The service that
owns repositories and pull requests is the **repo host** — `RepoRef.host` names
it, and `seams/vcs.py`'s docstring already called it that.

**The credential never enters a URL, and this is load-bearing.** The obvious
design is `https://x-access-token:…@github.com/o/r.git`, and it has two costs that
are easy to miss: argv is visible to `ps`, and git **records the URL it cloned
from in the clone's own config** — so the mirror would be a credential at rest
under `$FORGEPILOT_HOME`, readable long after the run that made it and travelling
with any copy of the directory. Instead the URL stays ordinary and the token
arrives through `url.<base>.insteadOf` in the **environment**
(`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`), which git reads and the filesystem never
sees. `redact()` is the second line of defence, because git quotes URLs back in
its failures and the exception is what reaches the session log.

⚠️ **And the rewrite only applies to a network URL.** A `file://` remote has no
host to present a credential to; rewriting it produces a URL that cannot resolve.
The first version injected unconditionally and turned every clone against the
test suite's stand-in host into `fatal: '//x-access-token:***@C:/…' does not
appear to be a git repository` — six tests red at once, and the message pointed at
the fixture rather than at the rewrite.

**The two halves are built differently, on purpose:**

- **Git speaks for itself.** `clone`/`fetch`/`push` are git, and git reaches a
  repository over any URL including `file://`. Only `remote_url` knows it is
  GitHub — which is why the tests run **real clones and real pushes** against a
  bare repository on disk instead of against a stand-in for git. A stand-in would
  be testing a fiction, the same argument as the ripgrep provider having no
  Python fallback.
- **The pull request is an HTTP call**, so it is one thin method behind an
  injectable transport with the body and the error parsing as pure functions.
  Same split as `providers/llm/openai_compat.py`, for the same reason.

**The refspec is explicit, and the URL is named directly rather than `origin`.**
`git clone --mirror` sets `remote.origin.mirror=true` and a `+refs/*:refs/*`
refspec, which makes `git push origin <branch>` publish **every** ref — tags,
other people's branches, deletions. `push(repo, branch)` means one branch, so the
refspec is `refs/heads/<branch>:refs/heads/<branch>` and the URL is spelled out.

**A bare repository cannot `checkout -b`.** The mirror is bare, so the only way
to put a commit on a new branch in it is `git worktree` — which is exactly what
`workspace.prepare` does for a session, so the tests and the production path agree
about the shape of the thing being pushed. The first version of those tests asked
for `checkout -b` and got *"this operation must be run in a work tree"*.

Other things worth keeping:

- **`ensure_mirror` now has a caller and it is called unconditionally.**
  `run_task` calls it before `workspace.prepare`: a worktree cannot be cut from a
  repository that is not on the machine yet. Not gated on a capability —
  `ensure_mirror` is a *mandatory* Protocol method, not an optional extra, and the
  shipped providers implement it as a no-op because their mirror is the local
  checkout. That is also why nobody noticed it had no caller for the whole of P1.
- **`NoToken` rather than an anonymous request.** An empty credential against a
  private repository returns **404**, and a 404 reads as "that repository does not
  exist" — which sends the reader hunting for a typo for as long as it takes them
  to think of the token.
- **GitHub's own error text is what gets reported.** A 422 from the pulls
  endpoint means the head branch is not pushed yet, or a pull request for it
  already exists, and the body says which. Replacing that with "request failed"
  discards the only part that shortens the diagnosis.
- **The mirror path is derived, and three rows must agree on it.** It is
  `<mirror_root>/<host>/<namespace>/<name>`, and `workspace`/`retriever` must be
  configured to point at exactly that directory. Nothing checks that they agree.
  It cannot, without changing a seam: `Workspace.prepare(repo, base_ref,
  session_id)` names a repository rather than a directory, and a provider may not
  import another provider. So composition happens in configuration — where a
  mistake is a pathspec error, not a silent wrong answer.
- **`httpx` is now declared in `pyproject.toml`, and it was a real defect that it
  was not.** `providers/llm/openai_compat.py` imports it on the first model call,
  so a clean install without it **could not talk to a model at all**. Nobody
  noticed because this machine had httpx for unrelated reasons. Every undeclared
  dependency has that shape: it works until the one machine that matters.

**What the tests do not cover, and the live test does.** Everything above is
exercised without a network. What no local test produces is whether GitHub
*accepts* what this sends — the error shapes, the `draft` flag, the `head` ref
format and the permission model are facts about the host. So
`tests/providers/test_github_vcs_live.py` is gated on `FORGEPILOT_GITHUB_TOKEN`
plus `FORGEPILOT_GITHUB_LIVE_REPO`, **pushes a branch and opens a real pull
request**, and does not run by default. It names its target explicitly rather than
defaulting, because a test that picks its own target eventually runs against
something nobody chose. **Never paste a token into a conversation or a file in
this repository.**


## The decision authority, actually connected (P1-9)

`PolicyService.decide()` was *"the only decision authority in the system"*, and
nothing in `src/` called it. Two halves were missing, and they are not the same
half:

| | where | covers |
|---|---|---|
| **A** | a `tools/pre-execute` listener | what the **model** asks for — `read_file`, `write_file`, `run`, `code_search` |
| **B** | `run_task`, before it pushes or opens a PR | what the **harness** does on its own |

⚠️ **B is the one the configuration was about.** `ask_operations: [open_pr]`
named precisely the operation that never reaches the mechanism it drives:
pushing a branch and opening a pull request are *not tool calls* — no `tool/call`,
no armed capability, no `describe_targets` — so no pre-execute listener could ever
see them. Wiring only A would have left the documented claim false. It was written
down wrongly in `examples/stat-fix/github/README.md` until it was checked.

### ⚠️ `default: deny` in the shipped profile was a trap

It read `deny`, reasoning that "an operation nobody granted is not permitted by
omission" — but **granting is not this service's job.** It never sees the grant:
that is `ExecutionConstraints`, narrowed by `derive`, enforced by the guards and
`ToolDefinition.required_operation`. A `deny` here means **every tool call
refused**, because `read_file` maps to `read_code`, which nobody listed.

Nobody noticed because nothing called `decide()`. It is `allow` now, and
`tests/identity/test_policy_is_wired.py::test_the_shipped_profile_does_not_refuse_every_tool_call`
asserts it **behaviourally** — against what `decide` returns for an ordinary
operation, not by grepping the YAML, which would pass on a misspelled key.

Policy's job is the *call-time* question the grant cannot answer: does this
particular act need a person?

### Things worth keeping

- **`CallPreview.required_operation`.** Without it a listener could not ask "may
  this principal do this *kind* of thing" — the one phase whose job is to decide
  had nothing to decide *about*. It is the tool's **declared** operation, never
  inferred from its effects.
- **`PreExecuteScope.principal`.** Data, not a capability: a frozen value object
  with no callable and no handle. `ExecutionConstraints` already carried the id and
  tenant, so no identity information is new — this hands over the object rather
  than making every listener rebuild one. `test_pre_execute_is_pure` asserts the
  scope still holds nothing that can act, and now also that the new field is not
  callable.
- ⚠️ **Policy abstains on `ALLOW`; it does not return `ALLOW`.** The chain is
  *first opinion wins* and this listener is registered first, so returning `ALLOW`
  ends the chain on every ordinary call and **silently disables every other
  pre-execute listener in the process.** That is exactly what the first version
  did, and it was noticed because listeners that tests registered stopped being
  called. Policy's `allow` means "I do not object", and a listener that does not
  object must not be the last word. Abstaining also gives the right asymmetry:
  another listener can still tighten to deny, and none can loosen a refusal.
- **A pipeline with no principal refuses every call**, by name. Skipping would be
  the wrong way round for the phase whose whole job is to decide.
- **`assemble_session` requires `policy` and raises without it.** An undeclared
  read on the *root* context returns `None` rather than raising, so a profile with
  no `policy` row arrives as `None` — and "no policy, so nothing to check" is
  backwards. Rigs that build a pipeline by hand (rather than through
  `assemble_session`) must pass `principal=`; they load `PolicyServicePlugin` with
  `default: allow` where they need one at all.

### The outward-facing step, and what refusal means

`runtime/authority.py` is where B lives. It is **not a second decision
authority**: it calls the same `decide` and maps the same three outcomes; what it
adds is the `ask` half, which the pipeline gets from its approval phase and which
has no equivalent outside it. `ALLOWED_ONCE` is the only permissive approval
outcome, the prompt is flushed before the answerer is awaited, and `summary` is
written by the harness from what happened — never phrased by the model.

Push and pull request are authorised **together, on `OPEN_PR`**, because they are
one publication step: nothing is pushed unless a pull request is about to be
opened. There is no `PUSH` operation, and inventing one here would be this
function deciding what the coarse operation set means.

**A refusal lands at `READY`, not `FAILED`.** The work is done, committed and
verified; the only thing missing is permission to publish it. That is what `READY`
already means — and the draft goes back with the outcome so a person can raise it
by hand. Verifying this by removing the gate makes the task report `DELIVERED` on
an unanswered ask.


## A failed check now says which test failed (P7, 2026-10-20)

`run_checks` used to copy whatever the runner printed into `CheckResult.output`,
which is written to the session log and replayed into every later request. A task
whose checks fail four times therefore paid for four full tracebacks, and the one
line that matters sat somewhere in the middle of each.

Two mechanisms in `runtime/checkoutput.py`:

- **`bound()`** keeps the **head and the tail**, with a marker naming how many
  characters were dropped (`MAX_CHECK_OUTPUT_CHARS = 20_000`). Truncating only the
  tail is the tempting version and the wrong one: the tail is where the failing
  assertions and the runner's summary live.
- **`failed_tests()`** extracts ids from pytest's short summary and unittest's
  `FAIL:`/`ERROR:` lines, de-duplicates them, and caps the list at 50. It
  recognises two runners and **guesses at nothing else** — a fabricated test name
  sends the agent to a file that does not exist, which is worse than the blob it
  replaced.

`CheckResult.meta["failedTests"]` carries the ids, and the output **begins** with
`failing tests: …` so the model cannot miss it; the `task/check` event already
carries `meta`, so an auditor and the eval read the same list without parsing
prose.

Limits, stated rather than implied: other runners (go test, cargo test, jest)
yield no ids until someone adds a pattern for them; the bound is a character
count, not a semantic reduction; and a check that fails by exit code with no
parseable line is still reported as a failure with the raw output.

**No JUnit XML reader, and that is a change from the plan.** The plan named one, and
it is the wrong first move: a runner writes JUnit XML only when it has been told to, so
the reader would work on a project that had already configured its runner and return
nothing everywhere else — while the summary line is already in the output `run_checks`
captures, for both runners this repository uses, with no configuration at all. The
sequence when a deployment needs it is a `--junitxml` path in `project.yaml` first, the
reader second; a reader with nothing to read is a field with no consumer.

## A flaky check, and why the retry is of the *command* (P7-2, 2026-10-20)

The eval matrix had been reporting failures that were never in the change: a test
that depends on ordering, on the wall clock, on a port. One failing run failed the
whole task, and the repair loop then spent a model turn — the expensive retry —
looking for a bug that did not exist.

`project.yaml: check_attempts` (1–3, default **1**) asks the command again instead.
The decision lives in `runtime/repair.py::run_checks_until_stable`, and it takes
the **whole check decision** as a callable rather than a command list, because task
mode's decision can be two trees combined (`combine_checks`): retrying each command
of each tree independently would pair one tree's second attempt with the other
tree's first.

Three rules, each one a way this could lie instead:

- **Only a failure is retried.** `could_not_run` is a configuration fact and a
  timeout is a budget fact; neither improves by being asked twice.
- **Every attempt is in the log.** `run_checks` records its own `task/check` per
  attempt, and the deciding verdict — with `meta.flaky`, `meta.attempts`,
  `meta.firstExitCode` — is recorded once more after the retry. A retry that erased
  its first run would be a hidden failure.
- **A pass on a later attempt is flaky, not clean.** The pull request body says
  `passed on attempt 2 — the first run failed, so this check is flaky` rather than
  `passed`. A reviewer deciding whether to trust a green check has to know it was
  green the second time.

⚠️ **`check_attempts` is not `retry.max_attempts`.** The latter bounds how many
model turns may follow a failure; this one bounds how many times the command runs.
The config layer validates the value and refuses 0, 4, `true` and `"two"` by name —
clamping would leave a file saying something the harness does not do.

Measured: `tests/task/test_real_check.py::test_a_flaky_check_is_retried_and_the_retry_is_named`
runs a command that fails once and passes on its second run, and asserts the
delivery, the flaky meta, the log event and the body line. Setting the wiring back
to `attempts=1` turns it red with `ReplayLLM ran out of scripted turns: request #3`
— the second turn is the cost the key avoids, which is the whole argument for it.

## P29 — the body cites its log (2026-10-25)

A pull request body is a claim about work that happened elsewhere, and until this
milestone it was assembled from values passed in memory — `checks`, `changed`, `diff`.
The values were right; what was missing was the **pointer**, so a body that drifted from
the log looked exactly like one that had not.

- `runtime/claims.py::claims_for(events, repo=slug)` folds `task/check` and `task/state`
  events into `Claim(text, event, seq)`. A claim cites an event, not a variable: if the
  log does not contain the fact, no claim is produced, and the section is simply absent.
- `render_evidence(session_id, claims)` prints `## Evidence` with the session id and one
  line per claim (`- \u0060pytest -q\u0060 — passed — event 12 (\u0060task/check\u0060)`), placed before
  the diff so the diff stays the last thing in the body.
- `_rewrite_published_bodies` (the M21 sibling rewrite) is now the **last** thing a run
  does before `flush()`, and it runs for single-repository deliveries too. Both changes
  have the same reason: the commit and the run's verdict are events a delivery had not
  written when its body was first assembled. Rewriting before the verdict append is how
  a body ends up citing everything except the sentence a reviewer needs.
- The guard is falsifiable rather than decorative:
  `tests/task/test_task_mode.py::test_every_claim_in_the_body_cites_an_event_in_the_log`
  parses every `event N (\u0060kind\u0060)` out of the body, looks the seq up in the log and
  compares the kind. A citation that does not resolve fails the test.
- **A failed rewrite still does not fail the task.** The pull request exists and the
  change is in it; the body is the opening one, and `task/published` with
  `updated: false` is where a reader learns that.
