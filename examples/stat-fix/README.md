# stat-fix — the whole task workflow, on a real repository

One ticket, one repository, one bug that is actually there. Nothing in this
example is a double: the checkout is a git repository, the search runs ripgrep
over it, the model is the one in your profile, the check is a real `pytest`, and
the deliverable is a real commit with a real diff.

It exists because "does the task workflow work?" was, for a long time, answerable
only from inside a test. `run_task` was written and covered, and the first time
anyone ran it end to end it took a driver script living outside the repository.
An example only its author can run is not an example.

## Run it

From the repository root:

```bash
# 1. make the fixture checkout, and check that it is broken
.venv/Scripts/python.exe examples/stat-fix/prepare_repo.py

# 2. point the patch at this machine (prints three export lines)
eval "$(.venv/Scripts/python.exe examples/stat-fix/prepare_repo.py --export)"

# 3. run the ticket
.venv/Scripts/python.exe -m forgepilot --config-dir configs \
    --patch   examples/stat-fix/patch.yaml \
    --project examples/stat-fix/project.yaml \
    --task BUG-1 --token demo-token --allow-uncontained-exec
```

The model credential comes from the gitignored `.env.local` at the repository
root — see the "Running it locally" section of `CLAUDE.md`. There is no key in
any file here.

### What you should see

```
plan      : queued  (planned)
category  : bug  (confidence 0.95)
tools     : bugfix -> ['read_file', 'code_search', 'write_file', 'run']
repos     : ['git.local/demo/stats']
classifier calls: 1

state     : ready   (the change is committed on the local branch ... )
branch    : forgepilot/session-...
changed   : ['stats.py']
check 1   : pass  exit=0
             | 8 passed in 0.02s

--- the draft that would be raised ---
Fixes BUG-1.
...
-    for index in range(len(values) - window):
+    for index in range(len(values) - window + 1):
```

**`ready`, not `delivered`.** That is the honest terminal state here: the checks
passed and the work is committed on a local branch, but this deployment has no
*repo host* — nothing that owns repositories and pull requests — so nothing was
pushed and no pull request exists. `TaskState.READY` says exactly that.

**For the same run ending in a real pull request, see `github/`.** That variant
swaps one row (`vcs`) for `GitHubVcsPlugin` and the task ends `DELIVERED`: the
branch is pushed and a draft PR is opened. It is the same fixture, the same
ticket and the same model — which is the point.

## The files

| File | What it is |
|---|---|
| `fixture/` | the repository's *sources* — committed as plain files, not as a checkout |
| `prepare_repo.py` | turns `fixture/` into a real git repository, and verifies the bug is really there |
| `tickets/BUG-1.yaml` | the ticket, as data — the `tickets` row reads this directory |
| `project.yaml` | repositories, the check command, what may be written |
| `patch.yaml` | the five provider swaps that make this a real deployment rather than a skeleton |

`fixture/` is not a nested checkout because that would be a submodule, which is a
second thing to keep in sync. `prepare_repo.py` builds the repository instead —
under `$FORGEPILOT_HOME/examples/stat-fix/` by default, so nothing lands inside
the working tree.

Re-running it is safe. A prepared checkout carries a `.forgepilot-example`
marker; `prepare_repo.py` recreates those and refuses to touch a directory it did
not make. `--export` never deletes anything, because it exists to be put inside
`eval "$(...)"`.

## Why the patch swaps five rows

Each swap is a thing the default profile *correctly* is not, and each one is a
blocker if you leave it:

| Row | Default | Why it has to change |
|---|---|---|
| `tickets` | in-memory fixtures, empty | `--task BUG-1` would report "no such ticket" — the mock is a test double, and a test is what populated it |
| `classifier` | always declines | a declined classification is `AWAITING_INFO`, so **every** ticket stops at a human and task mode is unreachable |
| `workspace` | copies files to a temp directory | no repository, so no branch, no commit, no diff — the delivery loop has nothing to attach to |
| `retriever` | ripgrep, but `roots: {}` | a shipped default cannot know your paths, so search raises `UnknownCheckout` |
| `vcs` | mock, which declares `pull_requests` | it answers with a fabricated PR reference, and the task would report `DELIVERED` on the strength of a pull request that does not exist |

The last one is worth pausing on. `LocalRepositoryVcsPlugin` is defined mostly by
what it *cannot* do: it declares `{"diff", "changed_files"}` and raises
`NoRepoHost` for everything else. That is what makes the run end at `READY`
instead of lying.

## What this does not cover

- **The sandbox is real but uncontained.** `providers/sandbox/local.py` executes
  commands and reports `enforcement: none`, honestly. On a Linux host the kernel
  sandbox (`bundles/kernel_sandbox`) claims `full` and this is not needed; on
  Windows there is no containment backend, which is why the command line passes
  `--allow-uncontained-exec` — a deliberate act that prints a warning, not a
  default.
- **No repo host.** See `ready`, above.
- **The tests are pinned, so editing them will not help.** Whatever the agent
  changes under `test_paths` — here `tests/` — is restored to the base revision
  for the duration of the check, then put back. If you want to see that happen,
  change the check command in `project.yaml` to something impossible and watch
  the agent eventually try the test instead; the check keeps failing and the
  output says why.

  Worth being precise about what this does and does not do: the agent's edit is
  *kept*, appears in the diff, and is named in the draft's body for the reviewer.
  It simply does not decide whether the checks pass. And it only covers paths the
  project calls tests — a build step that writes artefacts into the checkout
  still shows up as the agent's work.
