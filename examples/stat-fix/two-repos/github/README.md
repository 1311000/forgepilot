# Two repositories, one ticket, two pull requests

This is [../README.md](../README.md)'s deployment with the slugs pointing at a real
repo host. What it demonstrates is the one thing neither the local variant nor the
mock can: **two branches pushed and two pull requests opened by one run**, each
reviewable on its own, each body saying where the other repository stands.

| | local variant (`../`) | this one |
|---|---|---|
| slugs | `git.local/demo/*` | `github.com/1311000/test`, `github.com/1311000/test-reporting` |
| `vcs` | `LocalRepositoryVcsPlugin` | `GitHubVcsPlugin` |
| terminal state | `READY` — two drafts, nothing published | `DELIVERED` — two pushed branches, two pull requests |
| mirror | the checkout *is* the mirror | cloned and fetched from GitHub |
| opening a PR | `NoRepoHost`, refused | a real `POST /repos/…/pulls`, twice |

The second repository is self-contained on purpose: `reporting/windows.py` is a
copy of `stats.moving_average`'s off-by-one, taken along when the two were split
out of one repository. Each delivery's checks run in **that delivery's own
worktree**, so `reporting` cannot import `stats` — the defect really is in both
places, and a fix landing in only one leaves the other broken.

## Running it

```powershell
# 1. The second repository, made and seeded from its own fixture. The first one is
#    already seeded by the single-repository example; `--create` is idempotent, so
#    re-running either is safe.
.venv/Scripts/python.exe examples/stat-fix/github/seed_github.py `
    --slug github.com/1311000/test-reporting `
    --fixture examples/stat-fix/fixture-reporting --create

# 2. The three paths the patch needs (no network work here — `--export` only prints).
.venv/Scripts/python.exe examples/stat-fix/github/seed_github.py --export --shell powershell | Invoke-Expression
$env:FORGEPILOT_PYTHON = (Resolve-Path .venv/Scripts/python.exe).Path

# 3. The ticket. It names both repositories, so it plans two deliveries.
.venv/Scripts/python.exe -m forgepilot --config-dir configs `
    --patch examples/stat-fix/two-repos/github/patch.yaml `
    --project examples/stat-fix/two-repos/github/project.yaml `
    --task BUG-2 --token demo-token --allow-uncontained-exec
```

The credential is `FORGEPILOT_GITHUB_TOKEN` (`repo` scope), from the environment or
the gitignored `.env.local` — never in a file that gets committed, and never in a
URL. The model credential comes the same way; see `CLAUDE.md`.

## What a good run prints

Two delivery blocks, each with its own branch, commit, check result and pull
request URL, then one verdict:

```
state     : delivered   (delivered)
repository: github.com/1311000/test            branch forgepilot/session-…  pull req https://github.com/1311000/test/pull/N
repository: github.com/1311000/test-reporting  branch forgepilot/session-…  pull req https://github.com/1311000/test-reporting/pull/M
```

Open both pull requests: each body carries a section listing the sibling as this
pull request / `#number` / "committed on `<branch>` and verified, but not
published" / failed / not attempted. With one repository that section is absent,
and the body is byte for byte what it always was.

## What this still does not cover

An integration suite that needs **both** repositories in one directory. There is no
merged checkout and no per-repository `test` command: every repository runs the
project's one command in its own tree. That is the gap `docs/notes/task-delivery.md`
records, and building it is the work of whoever needs it — the seam does not
foreclose it.

## Why a third directory

`project.repos` is what `preprocess` scans ticket text against, so the ticket and
the project have to name the same repositories. `../project.yaml`'s ticket is a
correct ticket for the *local* deployment; pointing it at GitHub by rewriting a slug
at run time would make the ticket text and the project disagree. The cost is the one
`../../github/` already pays: a change to a shared row does not reach this file.
