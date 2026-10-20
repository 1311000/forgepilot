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
  commit, so it belongs on a clock and not on the push gate — with `--skip-editable`,
  because this repository is the editable install and is not on PyPI.

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


def test_the_pinned_kernel_is_ignored_by_the_bot_that_would_bump_it() -> None:
    """A deliberate pin and an open bot are two decisions pointing opposite ways.

    `cordis-port==0.1.5` is an exact pin: the kernel's semantics are what the 16 assertions
    in `tests/kernel/` hold, and its releases ship no changelog beyond a compare link, so a
    bump is an experiment rather than maintenance. Dependabot proposed 0.1.5 -> 0.1.7 on
    2026-10-19 and it was not taken (the diff touched `fiber.py` and the hook-disposal path
    in `events.py`). Without an `ignore` entry the bot proposes it every week, and a PR
    nobody intends to merge trains people to dismiss bot PRs — including the security ones.

    Read as a pair: while `pyproject.toml` pins `cordis-port` exactly, the bot must be told.
    When the pin becomes a range (or the kernel is evaluated and bumped), this test should be
    deleted with it rather than relaxed.
    """
    pyproject = tomllib.loads((FORGE / "pyproject.toml").read_text(encoding="utf-8"))
    pinned = [
        requirement
        for requirement in pyproject["project"].get("dependencies") or ()
        if requirement.split("==")[0].strip().lower() == "cordis-port" and "==" in requirement
    ]
    if not pinned:
        return
    config = _dependabot()
    ignored = {
        str(entry.get("dependency-name"))
        for update in config.get("updates") or ()
        for entry in update.get("ignore") or ()
    }
    assert "cordis-port" in ignored, (
        f"`pyproject.toml` pins {pinned[0]!r} exactly and dependabot is not told to ignore "
        f"it, so the bot will keep proposing a bump this project has decided is an "
        f"experiment. Either add the ignore entry (with its reason) or stop pinning."
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
    workflow = yaml.safe_load(
        (FORGE / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
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


def test_the_audit_is_handed_the_dependencies_and_not_this_package() -> None:
    """`--strict` fails on any distribution pip-audit did not audit — including ours.

    Two measured false starts, both the job's *first* execution (dispatch runs 46 and 48):

    - `pip_audit --strict`:
      `ERROR:pip_audit._cli:forgepilot: Dependency not found on PyPI and could not be
      audited: forgepilot (0.0.1)` — exit 1;
    - `pip_audit --strict --skip-editable`:
      `ERROR:pip_audit._cli:forgepilot: distribution marked as editable` — exit 1.

    So the exclusion has to happen *before* pip-audit sees the package: the job writes
    `pip freeze --exclude forgepilot` to a file and audits that. What is audited is exactly
    the dependency set a deployment installs; the package under test is not a dependency of
    itself, and a check that can never pass is a check nobody reads.
    """
    workflow = yaml.safe_load(
        (FORGE / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    lines = [
        str(step.get("run", ""))
        for job in (workflow.get("jobs") or {}).values()
        for step in job.get("steps") or ()
        if "pip_audit" in str(step.get("run", "")) or "pip-audit" in str(step.get("run", ""))
    ]
    assert lines, "no CI job runs `pip-audit`"
    for line in lines:
        assert "--strict" in line, (
            f"`{line}` drops `--strict`, which is what makes an advisory with no fixed "
            f"version a failure. Non-strict exits 0 on exactly the case worth reporting."
        )
        assert "--skip-editable" not in line, (
            f"`{line}` uses `--skip-editable`, which still counts as an unaudited "
            f"distribution under `--strict`: measured on dispatch 48 as "
            f"`distribution marked as editable`, exit 1."
        )
        assert "--requirement" in line or " -r " in line, (
            f"`{line}` must audit a requirement list, so the local package can be excluded "
            f"before pip-audit decides it was skipped."
        )
    joined = "\n".join(lines)
    assert "forgepilot" in joined and "freeze" in joined, (
        "the job has to build its requirement list with `pip freeze --exclude forgepilot`; "
        "otherwise pip-audit is asked to audit the editable install and `--strict` fails "
        "every week for a fact about the layout."
    )
