"""Every gate, in one command. Run this; CI runs this.

    python tools/check.py             # every gate

⚠️ **This lives in `tools/` rather than `scripts/`, and the difference is not cosmetic.**
On a case-insensitive filesystem — every Windows checkout — a `scripts/` directory here
*is* the `Scripts/` directory a deployment may already have. That is not a theory: adding
`scripts/` swept a pre-existing `Scripts/A.bat` into the same local directory, where
Explorer shows one folder and Linux would build two, and where `git add scripts` is a
pathspec that reaches files nobody meant to stage. `tools/` collides with neither, so the
name is chosen for the platform trap rather than for taste.

⚠️ **CI calls this file rather than repeating the commands**, for the same reason
`config.apply_layers()` is the single composition function: two lists of commands
drift, and the drift is invisible until the day a human's green run and CI's red one
disagree about the same commit. `--dump-config` and `--boot` cannot drift for that
reason; these must not either.

⚠️ **`ruff format` is now a gate, and it became one the only honest way.** On 2026-10-20
`ruff format` was applied to the whole tree in a commit whose entire content was layout
(**138 files, +1217/−1043**), and that commit is listed in `.git-blame-ignore-revs` so
`git blame` still lands on the change rather than on the reformat. Before that the tree
was unformatted, and gating it would have been a 138-file red that nobody had agreed to.
The sequence matters: format once, record the commit, *then* gate it — a gate added first
would have been switched off in the first week. New code is formatted by the gate rather
than by habit; `ruff format <paths>` still works for a single file.

⚠️ **The eval sweep is deliberately not a step here.** `evals/run.py --scripted`
produces a **rate, not a verdict**: it exits 0 whether every task passed or none did,
because a failing row is a result rather than an error. Turning that into a gate would
mean inventing a threshold, which is the same unfounded claim the eval set exists to
refuse. Reading a rate is a deliberate act — `evals/run.py`, then `evals/report.py`.

Stops at the first failure, with the command and its output labelled. That is the
convention `runtime/repair.run_checks` uses and for the same reason: a wall of output
from three tools makes the one that failed hard to find.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

FORGE = Path(__file__).resolve().parents[1]


def ruff() -> str:
    """The ruff executable, or a sentence about how to get it.

    Looked for **beside the running interpreter first**, then on `PATH`. A
    virtualenv's `Scripts`/`bin` is on `PATH` only while the venv is *activated*, and
    every command in this repository invokes the interpreter directly — which is
    exactly when it is not. Same three-step reasoning as the ripgrep provider's
    binary resolution, and it was learned in the same place.
    """
    beside = Path(sys.executable).parent / ("ruff.exe" if sys.platform == "win32" else "ruff")
    if beside.is_file():
        return str(beside)
    found = shutil.which("ruff")
    if found:
        return found
    raise SystemExit(
        'ruff is not installed. `pip install -e ".[dev]"` installs it, and pytest-cov '
        "with it — both are declared in pyproject.toml rather than assumed."
    )


def run(label: str, argv: list[str]) -> bool:
    print(f"\n=== {label} ===\n$ {' '.join(argv)}", flush=True)
    completed = subprocess.run(argv, cwd=str(FORGE), check=False)
    if completed.returncode != 0:
        print(f"\nFAILED: {label} (exit {completed.returncode})", file=sys.stderr)
        return False
    print(f"ok: {label}")
    return True


def untracked_files() -> set[str] | None:
    """Files `git status` reports as untracked, or `None` when git cannot answer.

    The rule this serves is already written down — *a test run must not write into
    the repository* — and until now it was defended only by pins on `tmp_path`
    (`tests/conftest.py`) and by a test asserting the pin is in place. A pin cannot
    see a provider that opens a file beside the tree it was pointed at, and that is
    how two zero-byte `tmp*` files came to sit at the root of this checkout: nothing
    in the gate was looking. Comparing the tree before the steps with the tree after
    them is the version of the rule that does not depend on anybody remembering.

    Only *untracked* entries are compared, deliberately. A modified tracked file is
    visible in `git status` to whoever looks, and a gate cannot tell an editor from
    a test — comparing modifications would fail a run because somebody saved a note
    while it was going, which is exactly how this check first reported a leak of its
    own author's edit. An untracked file is the shape the leak actually takes: a
    worktree or a cache appearing where nothing was before.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(FORGE), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        # Not a git checkout (a source tarball), or no git on the machine. The gate
        # still has its other steps; refusing to run because the *check* could not
        # run would make a green run depend on a tool nobody asked for.
        return None
    untracked: set[str] = set()
    for line in completed.stdout.splitlines():
        status, _, path = line[:2], line[2:3], line[3:]
        if status == "??" and path.strip():
            untracked.add(path)
    return untracked


def main(argv: list[str] | None = None) -> int:
    from forgepilot.console import use_utf8_output

    # Output that cannot be encoded should show a placeholder rather than abort a run
    # that has already done the work — this repository's own recorded lesson from a
    # GBK console.
    use_utf8_output()

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)

    steps: list[tuple[str, list[str]]] = [
        ("ruff check", [ruff(), "check", "."]),
        ("ruff format --check", [ruff(), "format", "--check", "."]),
    ]
    # `--cov` takes no value: the source comes from `[tool.coverage.run]` and the
    # threshold from `[tool.coverage.report] fail_under`. One place, so a local run and
    # CI cannot be measuring different things — and so raising the bar is a one-line
    # edit rather than a second list of commands to keep in step.
    steps.append(
        ("pytest with coverage", [sys.executable, "-m", "pytest", "tests/", "-q", "--cov"])
    )

    before = untracked_files()
    for label, command in steps:
        if not run(label, command):
            return 1

    after = untracked_files()
    if before is not None and after is not None:
        leaked = sorted(after - before)
        if leaked:
            print(
                "\nFAILED: the steps above wrote into the repository. Every file a "
                "test needs belongs under `tmp_path` — a checkout inside the tree is "
                "seen by `git status`, by the retriever, and by the next run:",
                file=sys.stderr,
            )
            for line in leaked:
                print(f"  {line}", file=sys.stderr)
            return 1

    print("\nAll gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
