"""The dependency audit is configured, not remembered.

Three things have to be in the tree for "we audit dependencies" to be a fact rather than a
habit, and each is read here by a test because a config file nobody checks is the same
defect shape this repository keeps recording:

- `.github/dependabot.yml` names **pip** and **github-actions**, on a weekly schedule —
  Dependabot that watches one ecosystem is a claim about the other that it cannot keep.
- `pyproject.toml` carries an `audit` extra naming `pip-audit`, so the tool a CI job needs
  is declared in the project rather than installed ad hoc by a workflow line.
- the weekly job in `.github/workflows/ci.yml` installs that extra and runs `pip-audit`,
  gated to `schedule`/`workflow_dispatch` — a vulnerability database changes without a
  commit, so it belongs on a clock and not on the push gate.

A local `pip-audit` run needs the network; this test does not, which is why it checks the
three declarations instead of the result.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

FORGE = Path(__file__).resolve().parents[2]


def _dependabot() -> dict:
    path = FORGE / ".github" / "dependabot.yml"
    assert path.is_file(), "no `.github/dependabot.yml`: nothing watches the dependencies"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_dependabot_watches_both_ecosystems_weekly() -> None:
    config = _dependabot()
    updates = config.get("updates") or []
    ecosystems = {str(entry.get("package-ecosystem")): entry for entry in updates}
    assert {"pip", "github-actions"} <= set(ecosystems), (
        f"dependabot watches {sorted(ecosystems)}. An ecosystem it does not watch is one "
        f"nothing updates, and the workflow's own actions are the ones that run with a "
        f"repository token."
    )
    for ecosystem, entry in ecosystems.items():
        schedule = entry.get("schedule") or {}
        assert schedule.get("interval") == "weekly", (
            f"{ecosystem} is on {schedule.get('interval')!r}; weekly is the cadence this "
            f"repository can act on, and anything faster becomes noise nobody reads."
        )


def test_the_audit_tool_is_a_declared_extra() -> None:
    pyproject = tomllib.loads((FORGE / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"].get("optional-dependencies") or {}
    audit = " ".join(extras.get("audit") or ())
    assert "pip-audit" in audit, (
        "no `audit` extra naming `pip-audit`. The CI job could still install it by name, "
        "but then the project does not say what auditing means and the job and a developer "
        "would be running different tools."
    )


def test_the_weekly_job_runs_it() -> None:
    workflow = yaml.safe_load((FORGE / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    jobs = workflow.get("jobs") or {}
    auditing = [
        name
        for name, job in jobs.items()
        if any(
            "pip-audit" in str(step.get("run", "")) or "pip_audit" in str(step.get("run", ""))
            for step in job.get("steps") or ()
        )
    ]
    assert auditing, "no CI job runs `pip-audit`: the extra exists and nothing calls it"
    for name in auditing:
        condition = str(jobs[name].get("if", ""))
        assert "schedule" in condition, (
            f"job {name!r} runs pip-audit without a schedule condition. A vulnerability "
            f"database changes without a commit, so the audit belongs on the weekly job; on "
            f"the push gate it is a flaky red that teaches people to retry."
        )
