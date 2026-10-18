# Two repositories, one ticket

This variant exists to show the one thing the single-repository example cannot: **the unit
of delivery is a `(session, repository)` pair, not a ticket.** One ticket names
`git.local/demo/stats` and `git.local/demo/reporting`; the run gives each one its own
working tree, its own branch, its own commit and its own pull request, and it verifies each
in that repository's own tree before moving on.

## Why the second repository is self-contained

`reporting-dashboard` does **not** import `metrics-stats`. It could not: each delivery's
checks run inside that delivery's own working tree, and there is no checkout holding both
repositories at once. So the window arithmetic in `reporting/windows.py` is a *copy* of
`stats.moving_average`'s — the same off-by-one, taken along when the two were split out of
one repository three years ago.

That is what makes two independent deliveries the honest shape rather than a convenience:
the defect really is in both places, and a fix that lands in only one of them leaves the
other broken. `BUG-2` says so in as many words.

⚠️ **What this does not cover:** an integration suite that needs *both* repositories in one
directory. There is no merged checkout and no per-repository `test` command — every
repository runs the project's one `test` command, in its own tree. A deployment that needs
the merged shape has to add it; the plan's D3 records the decision and
`docs/notes/task-delivery.md` carries it as an open gap.

## Running it

```powershell
# 1. Build both checkouts (the second one lands beside the first, as a sibling).
.venv/Scripts/python.exe examples/stat-fix/prepare_repo.py --into .tmp/m13-example --force
$env:FORGEPILOT_EXAMPLE_REPO    = 'D:\…\.tmp\m13-example\repo'
$env:FORGEPILOT_EXAMPLE_REPO_2  = 'D:\…\.tmp\m13-example\reporting'
$env:FORGEPILOT_EXAMPLE_WORKTREES = 'D:\…\.tmp\m13-example\worktrees'
$env:FORGEPILOT_PYTHON          = 'D:\…\.venv\Scripts\python.exe'

# 2. Run the ticket. `--export` prints exactly these lines for your shell.
.venv/Scripts/python.exe -m forgepilot --config-dir configs `
  --patch examples/stat-fix/two-repos/patch.yaml `
  --project examples/stat-fix/two-repos/project.yaml `
  --task BUG-2 --token demo-token --allow-uncontained-exec
```

The run ends `READY` with two drafts, or `DELIVERED` with two pull requests if the `vcs`
row is pointed at a real host. `github/` is that variant, ready to run and published once:
two draft pull requests against `github.com/1311000`, one per repository. `patch.yaml`
lists both slugs in `workspace.repos`, `retriever.roots` and `vcs.repos`; leaving one out is
how you get a run that plans two repositories and then cannot prepare the second.

## Why this is a directory of its own

Composing it with `../patch.yaml` was refused for two reasons, both recorded in
`project.yaml`'s header:

- a second `${FORGEPILOT_EXAMPLE_REPO_2}` in the shared `patch.yaml` would raise for every
  run that did not set it — the single-repository example and all ten eval tasks;
- declaring `git.local/demo/reporting` in the shared `project.yaml` would silently change
  which repositories another ticket resolves to, because ticket text is scanned against
  that list and nothing else.

The cost of copying is the same one `../github/` pays: a change to a shared row does not
reach this file.
