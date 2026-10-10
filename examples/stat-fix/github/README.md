# stat-fix on GitHub — the loop that ends in a pull request

The same bug and the same fixture as `..`, with one row changed: `vcs` is a real
repo host. Everything else in the run is the same, and the difference is the
whole point — the task ends at **`DELIVERED`** with a pull request, not at
`READY` with a draft and a sentence explaining that nothing was published.

## Run it

From the repository root:

```bash
# 1. a token with `repo` scope, in the gitignored .env.local — never in a file
#    that gets committed, and never pasted anywhere with an audience.
#
#    ⚠️ Git Bash. In PowerShell, `>>` writes UTF-16 and the loader reads UTF-8,
#    which used to produce `ValueError: embedded null character` naming neither
#    the file nor the cause. There:
#      Add-Content -Path .env.local -Value 'FORGEPILOT_GITHUB_TOKEN=...' -Encoding utf8
echo 'FORGEPILOT_GITHUB_TOKEN=...' >> .env.local

# 2. put the fixture on GitHub, and print what to export
.venv/Scripts/python.exe examples/stat-fix/github/seed_github.py

# 3. point the patch at this machine.
#    `--export` only prints — no token, no network. It is inside `eval`, and a
#    command whose output is shell assignments must not also be pushing, or a
#    flaky link leaves you with no variables and an error about the wrong thing.
eval "$(.venv/Scripts/python.exe examples/stat-fix/github/seed_github.py --export)"

# 4. run the ticket
.venv/Scripts/python.exe -m forgepilot --config-dir configs \
    --patch   examples/stat-fix/github/patch.yaml \
    --project examples/stat-fix/github/project.yaml \
    --task BUG-1 --token demo-token --allow-uncontained-exec
```

Step 4 runs the whole loop: `ensure_mirror` clones `1311000/test` into a bare
mirror, a worktree is cut from it, the model searches and edits, a real `pytest`
decides, the change is committed, the branch is **pushed**, and a **draft pull
request** is opened. Then:

```
state     : delivered   (delivered)
branch    : forgepilot/session-20261010T...
changed   : ['stats.py']
check 1   : pass  exit=0
pull req  : https://github.com/1311000/test/pull/N
```

## What is different from the local example

| | local | GitHub |
|---|---|---|
| `vcs` | `LocalRepositoryVcsPlugin` | `GitHubVcsPlugin` |
| repository slug | `git.local/demo/stats` | `github.com/1311000/test` |
| terminal state | `READY` — draft written, nothing published | `DELIVERED` — branch pushed, PR opened |
| `ensure_mirror` | the local checkout *is* the mirror | clones and fetches from GitHub |
| opening a PR | `NoRepoHost`, refused | a real `POST /repos/…/pulls` |

## Rehearsing it without a token

Every step of `seed_github.py` except the URL is ordinary git, so it can be run
against a local bare repository — no token, no network, and the same `init` →
`commit` → `remote add` → `push` sequence a real run performs:

```bash
git init --bare -b main /tmp/rehearsal/remotes/1311000/test.git
FORGEPILOT_GITHUB_TOKEN=unused .venv/Scripts/python.exe \
  examples/stat-fix/github/seed_github.py \
  --slug github.com/1311000/test \
  --remote-base file:///tmp/rehearsal/remotes \
  --into /tmp/rehearsal/seed

git --git-dir=/tmp/rehearsal/remotes/1311000/test.git log --oneline main
```

This is how the seed script itself was verified. What it does **not** verify is
GitHub: the API's error shapes, the `draft` flag and the permission model are
facts about the host, and `tests/providers/test_github_vcs_live.py` is the only
thing here that asks them.

## Three things worth knowing

**The credential never enters a URL.** The obvious design puts the token in the
remote — `https://x-access-token:…@github.com/o/r.git` — and it costs more than
it looks. argv is visible to `ps`, and git records the URL it cloned from in the
clone's own config, so the mirror would be a **credential at rest** under
`$FORGEPILOT_HOME` long after the run. Instead the URL stays ordinary and the
token travels through `url.<base>.insteadOf` in the environment. The token is
also scrubbed from every message git produces, because git quotes URLs back in
its failures.

**A pull request is an outward-facing act, and nothing stops to ask you.**
`run_task` decides when it happens — after the checks pass — and the model has no
`open_pr` tool, so a pull request is not something the agent can do on its own.
What gates it is `ExecutionConstraints`: a caller without `open_pr` is refused.

⚠️ **The base bundle's `ask_operations: [open_pr]` does not apply here.** It is
read by `PolicyService.decide()`, and nothing in `src/` calls `decide()` — the
policy service is loaded, tested and consulted by nobody. So this run **will open
a pull request on the target repository without asking**, and the flag that looks
like it should prevent that has no effect. This was written down wrongly here
once, and corrected after checking. See the defect table in `CLAUDE.md`.

**The three paths must agree.** The mirror lives at
`<mirror_root>/<host>/<namespace>/<name>` — derived by the `vcs` provider — and
`workspace` has to cut its worktree from exactly that directory, so the same path
is spelled out in the `workspace` and `retriever` rows too. Nothing checks that
the three agree. It cannot, without changing a seam: `Workspace.prepare` names a
repository rather than a directory, and a provider may not import another
provider. So composition happens in configuration, where a mistake is a pathspec
error rather than a silent wrong answer.

## If it does not work

- **`NoToken`** — `FORGEPILOT_GITHUB_TOKEN` is unset. It is read from the
  environment on purpose; a token in a config file lands in `git` and in
  `--dump-config`.
- **`fatal: could not read from remote`** — the token lacks `repo` scope, or the
  repository name is wrong. `seed_github.py` reports both redacted.
- **`GitHub returned 422 — Validation Failed`** — the head branch is not on the
  host. Usually the push failed first and its error was above this one.
- **`no such ticket: 'BUG-1'`** — the `tickets` row points at
  `examples/stat-fix/github/tickets`, so run from the repository root.
- **`no repository could be determined`** — the ticket names a repository the
  project does not list. `preprocess` only recognises repositories the project
  knows, so the slug in the ticket and the slug in `project.yaml` have to match
  exactly.
