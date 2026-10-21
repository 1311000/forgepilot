"""Scaffold a task-mode deployment for one SWE-bench instance (P13 driver).

The in-loop check is a stub (`cmd /c exit 0`) on purpose: this box cannot run the
instance's own tests — they need a python:3.10 container, and docker refuses a child
process (it answers the shell and denies the npipe to Python). The oracle is the
container run, applied to the produced diff afterwards by `tools/swebench_grade.py`.

The tool set has no `run` on purpose too: `python -m pytest` on this interpreter is
3.14 against a 5.0-era tree, and a model that decides to "just run the tests" would
spend the whole budget on a wall it cannot climb.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".tmp" / "swebench_lite.jsonl"
REPOS = ROOT / ".tmp" / "swebench-repos"
PROJ = ROOT / ".tmp" / "sbproj"
MIRROR = ROOT / ".tmp" / "sbmirror"
SLUG = "github.com/astropy/astropy"


def load(instance_id: str) -> dict:
    for line in DATA.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["instance_id"] == instance_id:
            return row
    raise SystemExit(f"no instance {instance_id!r} in {DATA}")


def mirror(instance_id: str) -> Path:
    MIRROR.mkdir(parents=True, exist_ok=True)
    target = MIRROR / instance_id
    if (target / ".git").exists():
        return target
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(REPOS / instance_id), str(target)],
        check=True,
    )
    return target


def main() -> int:
    instance_id = sys.argv[1] if len(sys.argv) > 1 else "astropy__astropy-12907"
    row = load(instance_id)
    repo = mirror(instance_id)
    PROJ.mkdir(parents=True, exist_ok=True)
    (PROJ / "tickets").mkdir(exist_ok=True)
    ticket = PROJ / "tickets" / f"{instance_id}.yaml"
    ticket.write_text(
        "id: " + instance_id + "\n"
        "title: " + row["problem_statement"].splitlines()[0][:200] + "\n"
        "description: |\n"
        + "\n".join("  " + line for line in row["problem_statement"].splitlines())
        + "\n\n  Reproduction steps: run the test named in the report below on this checkout.\n"
        "  Expected result: the test passes.\n"
        "  Actual result: it fails as described.\n"
        "\n  This workspace has no way to run the repository's tests (they need a "
        "container it does not have). Read the code, make the smallest correct change, "
        "and stop. A test file that exercises the behaviour will be run by the grader; "
        "do not add or edit tests.\n"
        "status: new\n",
        encoding="utf-8",
    )
    project = PROJ / "project.yaml"
    project.write_text(
        f"""name: swebench-{instance_id}
repos:
  - {SLUG}
base_ref: main
test:
  - cmd
  - /c
  - exit
  - "0"
test_paths: []
writable_paths: .
allowed_operations: [read_code, write_code, run_tests, open_pr]
network: allow
retry:
  max_attempts: 1
tool_sets:
  default: [read_file, list_dir, glob, code_search, read_code, edit_file, write_file]
category_tool_sets:
  bugfix: default
budget:
  max_tokens: 600000
  max_seconds: 3600
""",
        encoding="utf-8",
    )
    mirror_root = str(repo).replace("\\", "/")
    patch = PROJ / "patch.yaml"
    patch.write_text(
        f"""- id: classifier
  plugin: forgepilot.providers.classify.llm:LLMClassifierPlugin
  config:
    model: ${{FORGEPILOT_LLM_MODEL:-deepseek-flash}}
    max_tokens: 2048
  tier: trusted

- id: tickets
  plugin: forgepilot.providers.tickets.file:FileTicketSourcePlugin
  config:
    root: .tmp/sbproj/tickets
  tier: trusted

- id: workspace
  plugin: forgepilot.providers.workspace.git_worktree:GitWorktreeWorkspacePlugin
  config:
    repos:
      {SLUG}: {mirror_root}
    worktree_root: .tmp/sbworktrees
    branch_prefix: forgepilot
  tier: trusted

- id: retriever
  plugin: forgepilot.providers.retrieve.grep:GrepRetrieverPlugin
  config:
    roots:
      {SLUG}: {mirror_root}
  tier: trusted

- id: sandbox
  plugin: forgepilot.providers.sandbox.local:LocalProcessSandboxPlugin
  config:
    max_output_bytes: 64000
  tier: trusted

- id: vcs
  plugin: forgepilot.providers.vcs.local:LocalRepositoryVcsPlugin
  config:
    repos:
      {SLUG}: {mirror_root}
  tier: trusted

- id: principal
  plugin: forgepilot.identity.principal:PrincipalServicePlugin
  config:
    tokens:
      - token: demo-token
        principal_id: demo
        tenant_id: local
        teams: []
        operations: [read_code, write_code, run_tests, read_ticket, open_pr]
        repos:
          - host: github.com
            namespace: astropy
            name: astropy
        repo_prefixes: [github.com/astropy/*]
        writable_paths: ["."]
        network: allow
  tier: trusted
""",
        encoding="utf-8",
    )
    print(f"mirror:  {repo}")
    print(f"ticket:  {ticket}")
    print(f"project: {project}")
    print(f"patch:   {patch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
