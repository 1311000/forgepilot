# The repeated defect shape, and the seam table this project diverged from

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before adding a field, flag or capability that is supposed to be enforced**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## ⚠️ Divergence from the design doc's seam table (P1-2)

The doc's seam table puts `create_branch` / `commit` on **`vcs`**:

```
| `vcs`       | ensure_mirror() / create_branch() / commit() / push() / open_pull_request() / diff() |
| `workspace` | prepare() / diff() / changed_paths() / dispose()   ← GitWorktreeWorkspacePlugin（每会话一个 worktree）
```

That table does not survive contact with `commit`'s own signature:
`commit(repo, branch, message, author, constraints)` **takes no path**, and the
workspace it must commit is a different object from the repository it names. The
mock hid this because it fabricates shas and never touches a tree.

**Decision (P1-2, done): a session's working tree belongs to `workspace`, so the
local git operations on it do too.** `workspace` is a real per-session
`git worktree` — which the doc's own tree already asks for
(`workspace/ git_worktree.py(先行)`) — and `vcs` keeps only what needs a remote:
`ensure_mirror`, `push`, `open_pull_request`. Two reasons this is the right split
rather than a convenience: it matches how the two things actually differ (a local
checkout vs a service that owns state), and it is the split the doc's *file tree*
already implies even though its table does not.

What the move cost, and what it bought:

- **`Workspace.commit(handle, message, author, constraints)`** — the one
  `Workspace` method that takes constraints, because it is the one that writes
  version-control history rather than rearranging a directory. The
  `allows_repo` / `write_code` checks that used to live on `vcs.commit` moved with
  it; losing them in the move would have been the easy mistake.
- **`vcs.create_branch` is gone, not moved.** `prepare()` already checks the tree
  out *onto* a branch, so the old call was creating a second name for a branch
  that already existed — the sort of thing that only looks reasonable while
  nothing is real.
- **`LocalTempWorkspacePlugin.commit` refuses** (`NotARepository`). It is a
  directory, not a repository; a fabricated sha would look like delivery, land in
  the session log as evidence, and be undetectable downstream.
- **The mock's `push` had to change.** It refused a branch it had not seen
  created — an artefact of `vcs` owning branch creation. Pushing a new branch is
  precisely how a remote learns about it, so the old check modelled the wrong
  thing.
- **`CommitAuthor` moved to `seams/refs.py`**, next to `CommitRef`: both seams
  produce commits, and a working tree that must import the remote-host seam to
  name an author has the dependency backwards.
- **`tests/conftest.py::make_origin_repo`** builds a real repository for the task
  rig. The task and review suites now run against real git end to end — branch,
  commit and worktree — which is what made the seam change verifiable at all.

**Vocabulary: 「宿主」 is taken.** In this codebase 宿主 means *the machine*
("a fact about **the host**", "host state"). The remote service that owns
repositories and pull requests is the **repo host** — which
`seams/vcs.py`'s docstring already calls it, and which `RepoRef.host` names.
Using 宿主 for both is exactly the ambiguity the Guard glossary entry warns
about; qualify it or use "repo host".

## The repeated defect shape, and where it has actually appeared

Fourteen rows now, in this codebase: **a field that looks enforced and is not.** It is
worth recognising on sight, because the cost is not the missing check — it is that
everything downstream behaves as though the check happened.

| Field | What was wrong |
|---|---|
| `ToolDefinition.required_operation` | recorded, snapshotted, shown in `--dump-config`, consulted by nothing |
| `Provenance.evidence_refs` | docstring said "no evidence ⇒ not a memory"; only `kind` was checked |
| `MemoryRecord` audience | `scope_ref` and `provenance.repo` treated as interchangeable; the wrong one was checked |
| `SandboxExecutionPolicy.mode` | accepted and never read (fixed — `PolicyNotEnforceable`) |
| `PullRequestDraft.draft` | set in one place, read in none, while the code did the opposite |
| `Vcs.capabilities()` | declared by every provider, read by nothing |
| `ResourceTargets.commands` | extracted by the `run` tool's `describe_targets`, read by no guard — the guards looked at `repos` and `paths` only. **Closed at M14**: the reader is `command_scope_guard` (`runtime/guards/builtin.py`), registered as `command-scope` in `default_guards`. It abstains until a deployment *declares* a list, and a line it cannot decompose into programs is refused rather than waved through |
| `_narrowed_paths` | a **filter** where the operation is an **intersection** — a grant of `"."` behind a project limited to `src/` produced *nothing writable*. Fixed at P1-11; every existing test used the same set on both sides, which is the one case the two agree on |
| `UsageTracker.cost_usd` / `wall_seconds` | the budget guard read both; **nothing wrote either, on any path**. Two of four budget dimensions were unreachable limits. Fixed — see `docs/notes/agent-loop.md` |
| `ForgePilotAgent.tracker` | `field(default_factory=UsageTracker)` and `create()` never passed one, so the loop counted into the agent's private tracker while the **guard** watched the scope's. The per-step check in `turn.py` used the agent's and worked, which is why it looked fine |
| `max_steps` | checked one iteration **early** (`if step == max_steps - 1: break`), so it yielded `max_steps - 1` steps — `DEFAULT_MAX_STEPS = 12` meant 11. And `run_task` never passed it, so a profile's ceiling reached chat but not task mode |
| ~~`PolicyService.decide()`~~ | the whole service was unwired — **wired at P1-9**; see `docs/notes/task-delivery.md` |
| `SandboxExecutionPolicy.network` | accepted and never read, **one branch over from `mode`**. It defaults to `DENY`, every caller threads its constraints through (`builtin.run`, `repair.run_checks`), and the model's scope section stated `Network: deny` — while `local.py` spawned the command with an unrestricted network reach. Fixed at P4-1; see `docs/notes/sandbox.md`. ⚠️ **This one is worse than the others in the table**, because the false statement was made *to the model*, which then reasoned from it |
| `Plan.repos` / `plan.repos[0]` | `plan_task` filled the plan with **every** repository the ticket named (and, when it named none, with every repository the project declared) while `run_task` executed against `repos[0]`. A two-repository ticket was fixed in one of them and reported `READY` / `DELIVERED`. ⚠️ The worst instance of the shape so far, because the discarded half of a *cross-repository* change is exactly what nobody goes to look at. Fixed at P5-2; see `docs/notes/task-delivery.md` |
| `WorkspaceHandle` keyed on `session_id` alone | `prepare`/`dispose`/`_baseline` in both workspace providers keyed their handle cache on the session id, so the second `prepare(repo_b, …)` **in one session returned repo A's tree and repo A's branch** — and, the baseline being keyed the same way, reported B's changes as A's diff. The producer was already plural (`ProjectConfig.repos` is a tuple, `preprocess` returns every mention); the *key* was singular. Same shape one level down: a scope that looks per-something and is per-something-else, with no error anywhere. Fixed at M13 Phase A — `(session_id, repo.slug)`, with the branch and the directory name carrying the slug too — and the falsifier (`test_one_session_two_repositories_gets_two_worktrees_and_two_branches`, `test_two_repositories_in_one_session_get_two_workspaces`) is red under the old key. See `docs/notes/task-delivery.md` |

`capabilities()` is now load-bearing: `run_task` consults it before opening a pull
request. `PullRequestDraft.draft` was a **known** absence rather than an oversight —
a flag for a repo-host provider to send upstream, and there was no host provider.
It has one now, and `pull_request_payload` passes it through, so it moved from the
table's "gap" column to "enforced". The distinction is worth keeping: a field with
no consumer *because the consumer does not exist* is a gap; a field with a consumer
that ignores it is a defect.

**Twice the shape appeared in a document rather than a field, and both are pinned by
`tests/contract/test_the_workspace_instructions_fit_the_budget.py` now.**

- **`CLAUDE.md` was 188315 bytes while a harness injects the first 65536.** The
  repository's most authoritative document was read up to a mid-paragraph point, and the
  reader had no way to tell — the file read as authoritative while two thirds of it never
  arrived. Fixed by the split into this directory, and the budget is a test, not a
  resolution: a *note nobody reads* and a *rule nobody can find* are the same failure.
- **`docs/实现方案.md` (30 KB, the implementation plan) sat in the tree for the whole
  project and no instruction named it**, beside `ForgePilot文档.md` — which the front door
  did name. Two Chinese design documents, one referenced and one not, with nothing saying
  how they relate; the obvious reading was that the named one was the only one. The guard
  now fails on an orphan document, and on a reference to a path that is not there, which
  is the failure mode the split *created*: a front door that is mostly pointers is one
  where a rename leaves a reader following a dead end.

⚠️ **`PolicyService.decide()` was the largest one — a whole service rather than a
field, wired at P1-9.** For most of this project's life `grep -rn "\.decide("
src/` returned nothing, and the `policy` row's `default` and
`ask_operations: [open_pr]` were read by a function nothing invoked. What actually
enforced authority was `ExecutionConstraints` plus the guards plus
`ToolDefinition.required_operation` — and those work — while the finer-grained
per-operation allow/ask/deny design was built, tested to the letter and never
connected. See `docs/notes/task-delivery.md`.

`ResourceTargets.commands` was a **gap** of the same kind for most of this
project's life: nothing read it, because **there was no command guard** —
`permitted_operations` decided whether `run` may execute at all, and no policy
anywhere constrained *which* commands. That was a policy feature nobody had asked
for, not an oversight. **M14 closed it**, and the reason it was finally worth
closing is the one this paragraph used to give for leaving it: `run` reaches a
shell, so "may execute" was always a weaker sentence than it sounded.

`ExecutionConstraints.allowed_commands` is the list, `derive` narrows it
(`None` means *no policy declared*, and an empty set means *nothing permitted* —
the two cannot be one value, because narrowing is an intersection and
`∅ ∩ {git}` is `∅`), and `command_scope_guard` decomposes each line into the
programs it would start with `runtime/commandline.py::programs`, refusing any the
list does not name. **A line that cannot be decomposed is refused**: an allow-list
that cannot read a line must not bless it. With no list declared the guard
abstains, so every deployment that never asked for a command policy behaves
exactly as before.

Three limits, stated rather than implied (D5 of `~/.claude/plans/m14-command-guard-and-worktree-root.md`):

- **It does not inspect arguments.** A listed `git` may still be asked to push, and
  a listed `python` may still be handed `-c`. Which programs may start is this
  guard's question; what they do afterwards is the sandbox's.
- **A shell built-in or alias is just a name.** `cd`, `source` and `type` are
  compared like any other program and refused when the list does not name them —
  fail-closed and explainable, rather than a special-case list that would have to
  be kept right.
- **No command substitution.** `$(…)` and backticks make a line unattributable, so
  a declared list refuses it.

⚠️ **`run_checks` never goes through this guard, and that is deliberate.** The
`build`/`test`/`lint` lines in `project.yaml` are written by a human and handed
straight to the sandbox by `runtime/repair.py`; they are not tool calls, so no
guard sees them. `project.yaml`'s `commands:` is a *different* key from `test:`
for the same reason — one is context the harness executes, the other is a
permission the project grants a model.


## Unverified claim in the design doc

The doc cites a **+32% end-to-end delivery-success improvement** over a generic Claude Code workflow. The doc itself concedes the eval platform/version, success criteria, sample size, comparison conditions, and whether 32% is relative or percentage-points are all unspecified, and that it does not establish the current design achieved it. **Do not repeat this number in any material** until it is something we measured ourselves. Design decisions here are justified by invariants and threat models, not by an expected lift.
