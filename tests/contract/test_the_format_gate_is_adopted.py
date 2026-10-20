"""The format gate was adopted with its history intact, and both halves are read here.

Adopting `ruff format` as a gate has two preconditions, and either one missing turns the
gate into a nuisance:

- the one-time reformat has to be *findable* as such, so `git blame` can skip it — otherwise
  every line of the tree starts pointing at a whitespace sweep and the history stops being
  readable exactly when someone is trying to find out why a line is the way it is;
- the gate has to be *in the gate* — `.git-blame-ignore-revs` alone records an intention,
  and `ruff format --check` is what makes the next unformatted line red.

Measured 2026-10-20: the sweep was 138 files, +1217/−1043, and the full suite passed on it
(1886 passed, 254 skipped), which is what "layout only" means here rather than a claim.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

FORGE = Path(__file__).resolve().parents[2]
IGNORE_REVS = FORGE / ".git-blame-ignore-revs"
WORKFLOW = FORGE / ".github" / "workflows" / "ci.yml"

_SHA = re.compile(r"^[0-9a-f]{40}$")


def _ignored_revisions() -> list[str]:
    assert IGNORE_REVS.is_file(), (
        "no `.git-blame-ignore-revs`: the one-time reformat is in the history and `git "
        "blame` will point every line at it. Add the file (and run `git config "
        "blame.ignoreRevsFile .git-blame-ignore-revs`)."
    )
    revisions = [
        line.strip()
        for line in IGNORE_REVS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert revisions, "`.git-blame-ignore-revs` names no revision"
    return revisions


def _require_history() -> None:
    """Refuse a shallow clone by name, because absence is not evidence here.

    `actions/checkout` fetches one commit by default, and in such a clone `cat-file -e` on
    a revision that *is* in the history answers 128 — indistinguishable, from inside the
    suite, from a revision that never existed. The workflow fetches the full history for
    the job that runs this file (`fetch-depth: 0`); when someone strips that again, this
    refuses with the cause instead of accusing the file of naming a commit that is real.
    """
    shallow = subprocess.run(
        ["git", "-C", str(FORGE), "rev-parse", "--is-shallow-repository"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert shallow.stdout.strip() != "true", (
        "this checkout is shallow, so `.git-blame-ignore-revs` cannot be verified: a "
        "revision that its history contains is absent from this clone, which reads exactly "
        "like a revision that never existed. Fetch the history — `actions/checkout` with "
        "`fetch-depth: 0`, or `git fetch --unshallow` — because a gate that reads history "
        "needs the history."
    )


def test_the_job_that_runs_the_gate_fetches_the_history() -> None:
    """The other half of `_require_history()`: the workflow is what makes it pass.

    A directed failure in the suite is better than a confusing one, but it is still a red
    build. This asserts the thing that keeps it green — the job that runs `tools/check.py`
    checks out with `fetch-depth: 0` — so removing that line fails here, naming the job,
    before CI spends a minute discovering it.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    runners = [
        name
        for name, job in jobs.items()
        if any("tools/check.py" in str(step.get("run", "")) for step in job["steps"])
    ]
    assert runners == ["check"], (
        f"expected exactly one job to run `tools/check.py`, found {runners}. The "
        f"fetch-depth assertion below is written against that job."
    )
    checkouts = [step for step in jobs["check"]["steps"] if "checkout" in str(step.get("uses", ""))]
    assert checkouts, "the `check` job does not check anything out"
    depth = checkouts[0].get("with", {}).get("fetch-depth")
    assert depth == 0, (
        f"the `check` job checks out with `fetch-depth: {depth!r}`, so it is shallow: "
        f"`tests/contract/test_the_format_gate_is_adopted.py` cannot resolve "
        f"`.git-blame-ignore-revs` in a one-commit clone and fails with a message about a "
        f"revision that does exist. Set `fetch-depth: 0`."
    )


def test_the_gate_checks_formatting() -> None:
    source = (FORGE / "tools" / "check.py").read_text(encoding="utf-8")
    assert '"format"' in source and '"--check"' in source, (
        "`tools/check.py` no longer runs `ruff format --check`. The reformat is in the "
        "history, so the next unformatted line has nothing stopping it."
    )


def test_every_ignored_revision_exists() -> None:
    _require_history()
    for revision in _ignored_revisions():
        assert _SHA.match(revision), (
            f"{revision!r} in `.git-blame-ignore-revs` is not a full 40-character sha. An "
            f"abbreviated sha there is silently ignored by git, so the sweep stays visible."
        )
        resolved = subprocess.run(
            ["git", "-C", str(FORGE), "cat-file", "-e", f"{revision}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert resolved.returncode == 0, (
            f"{revision} is not a commit in this repository. A revision that does not "
            f"resolve makes the file look adopted while ignoring nothing."
        )


def test_an_ignored_commit_changes_no_behaviour() -> None:
    """Only a commit whose files are all Python can be a pure reformat.

    The check is deliberately weak about *what* changed and strong about *where*: a
    behaviour change smuggled into an ignored revision would be invisible to blame, so an
    ignored commit must not touch a test, a config, a fixture or a document.
    """
    _require_history()
    for revision in _ignored_revisions():
        shown = subprocess.run(
            ["git", "-C", str(FORGE), "show", "--name-only", "--format=", revision],
            capture_output=True,
            text=True,
            check=False,
        )
        paths = [line for line in shown.stdout.splitlines() if line.strip()]
        assert paths, f"`git show` produced no files for {revision}"
        foreign = [path for path in paths if not path.endswith(".py")]
        assert not foreign, (
            f"ignored revision {revision} touches non-Python files: {sorted(foreign)[:5]}. "
            f"A reformat commit is layout; anything else in it is invisible to `git blame`."
        )
