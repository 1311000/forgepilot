"""Turn ``fixture/`` into a real git repository, and print what to export.

Why this exists: ``GitWorktreeWorkspacePlugin`` makes a ``git worktree`` per
session, and a worktree needs a repository to hang off. The fixture is kept in
this repository as plain files — a nested checkout would be a submodule, which is
a second thing to keep in sync — so the repository is *made* here, once.

Re-running is safe and is the normal thing to do after a run has left branches
and worktrees behind: a prepared checkout carries a marker file, and this
recreates exactly those and refuses to touch anything else. A cleanup script that
will ``rm -rf`` a directory it did not create is a cleanup script that eventually
deletes somebody's work.

It finishes by running the project's checks **once**, before any agent is
involved. A fixture whose planted bug has quietly stopped failing produces a demo
that "passes" for no reason, and the only way to know is to look.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixture"
MARKER = ".forgepilot-example"

#: Files that are ours. Anything not here must be deleted, or a stale file from an
#: older run stays in the checkout and the diff has a change nobody made.
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".git")


def _default_root() -> Path:
    home = os.environ.get("FORGEPILOT_HOME")
    base = Path(home) if home else Path.home() / ".forgepilot"
    return base / "examples" / "stat-fix"


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} failed in {cwd}:\n{completed.stderr.strip()}"
        )
    return completed.stdout


def assignment(name: str, value: str, shell: str) -> str:
    """One line that sets ``name`` in ``shell``.

    The two shells are not interchangeable, and pretending otherwise is how
    somebody pastes `eval "$(...)"` into PowerShell and is told `eval` is not
    recognised. `export` is not a PowerShell verb either, so the output has to
    differ rather than merely be wrapped differently.
    """
    quoted = value.replace("'", "''")  # both shells escape a quote by doubling it
    if shell == "powershell":
        return f"$env:{name} = '{quoted}'"
    return f"export {name}='{quoted}'"


def prepare(repo: Path, *, recreate: bool, force: bool) -> str:
    """Materialise the fixture checkout. Returns ``created`` or ``reused``.

    ``recreate`` is off for ``--export``, and that is not a detail: the export
    path exists to be run inside ``eval "$(...)"``, which people put in scripts.
    A command that silently deletes the checkout every time it is sourced would
    discard a run in progress, and it would look like it had merely printed two
    variables.
    """
    from forgepilot.fs import remove_tree

    if repo.exists():
        if not (repo / MARKER).is_file() and not force:
            raise SystemExit(
                f"refusing to touch {repo}: it exists and has no {MARKER} marker, so "
                f"it is not a checkout this script made. Pass --into to use a "
                f"different directory, or --force if you are certain."
            )
        if not recreate:
            return "reused"
        remove_tree(repo)

    repo.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, repo, ignore=_IGNORE)

    # `-b main` rather than letting the machine's `init.defaultBranch` decide: the
    # project's `default_branch` is `main`, and a checkout whose branch is
    # `master` makes `worktree add ... main` fail with a message about a branch
    # that has nothing to do with the fixture.
    _git("init", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git(
        "-c",
        "user.name=ForgePilot Example",
        "-c",
        "user.email=example@localhost",
        "commit",
        "-m",
        "metrics-stats: statistics helpers for the reporting pipeline",
        cwd=repo,
    )
    return "created"


def verify(repo: Path) -> bool:
    """Run the checks before any agent does. Returns True if the bug is present.

    Not a test of this script — a test of the *fixture*. A planted defect that
    stopped failing would make every later run meaningless, and it would look like
    success.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    tail = (completed.stdout or completed.stderr or "").strip().splitlines()
    print(f"  checks   : exit {completed.returncode} (expected non-zero)")
    for line in tail[-3:]:
        print(f"             | {line}")
    return completed.returncode != 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--into",
        type=Path,
        default=None,
        help="where to create the fixture repository (default: under FORGEPILOT_HOME)",
    )
    parser.add_argument(
        "--worktrees",
        type=Path,
        default=None,
        help="where sessions' worktrees should live (default: beside --into)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="recreate the target even if it has no example marker. Deletes it.",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="print only `export` lines, for `eval \"$(... --export)\"`",
    )
    parser.add_argument(
        "--shell",
        choices=("posix", "powershell"),
        default="posix",
        help=(
            "which shell `--export` writes for. PowerShell has no `eval`, and "
            "`export` is not a PowerShell verb, so the two forms are not "
            "interchangeable - see the printed instructions for both."
        ),
    )
    args = parser.parse_args(argv)

    root = _default_root() if args.into is None else args.into.resolve()
    repo = root if root.name == "repo" else root / "repo"
    worktrees = (args.worktrees or repo.parent / "worktrees").resolve()

    # `--export` never destroys; an interactive run always starts clean. See
    # `prepare` for why that asymmetry is deliberate.
    action = prepare(repo, recreate=not args.export, force=args.force)

    exports = {
        "FORGEPILOT_EXAMPLE_REPO": str(repo),
        "FORGEPILOT_EXAMPLE_WORKTREES": str(worktrees),
        # The interpreter with pytest. `project.yaml` uses this for its `test:`
        # command, and it must be the same one - a `pytest` on PATH is often not.
        "FORGEPILOT_PYTHON": sys.executable,
    }

    if args.export:
        for name, value in exports.items():
            # Single-quoted: this is shell input, and a path is not something to
            # interpolate bare.
            print(assignment(name, value, args.shell))
        return 0

    print(f"{action} {repo}")
    ok = verify(repo)
    print(f"  tickets  : {HERE / 'tickets'}")

    print()
    if not ok:
        print(
            "WARNING: the checks PASSED on a fixture that is supposed to be broken.\n"
            "         Every run after this one would be meaningless. Look at\n"
            "         fixture/stats.py.",
            file=sys.stderr,
        )

    print("run the example with:\n")
    print("  Git Bash:")
    print('    eval "$(.venv/Scripts/python.exe examples/stat-fix/prepare_repo.py --export)"')
    print()
    print("  PowerShell (no `eval`; `export` is not a verb there):")
    print(
        "    .venv/Scripts/python.exe examples/stat-fix/prepare_repo.py --export "
        "--shell powershell | Invoke-Expression"
    )
    print()
    print("  Then, one line (`\\` continuation is bash-only):")
    print()
    print(
        "    .venv/Scripts/python.exe -m forgepilot --config-dir configs "
        "--patch examples/stat-fix/patch.yaml "
        "--project examples/stat-fix/project.yaml "
        "--task BUG-1 --token demo-token --allow-uncontained-exec"
    )
    print()
    print("(the model credential comes from .env.local - see CLAUDE.md)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
