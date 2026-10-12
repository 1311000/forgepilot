"""Turn the fixtures into real git repositories, and print what to export.

Why this exists: ``GitWorktreeWorkspacePlugin`` makes a ``git worktree`` per
session, and a worktree needs a repository to hang off. The fixtures are kept in
this repository as plain files — a nested checkout would be a submodule, which is
a second thing to keep in sync — so the repositories are *made* here, once.

**Two of them, since M13.** One ticket can cover several repositories and each one
is delivered on its own — one worktree, one commit, one pull request — so a demo of
that needs a second repository that is real: its own tests, its own branch, and its
own copy of the same defect (a copied loop is how the defect got into both). The
second checkout is built from ``fixture-reporting/`` beside the first, and is only
*used* by ``two-repos/``; the single-repository example goes on naming the first.

Re-running is safe and is the normal thing to do after a run has left branches
and worktrees behind: a prepared checkout carries a marker file, and this
recreates exactly those and refuses to touch anything else. A cleanup script that
will ``rm -rf`` a directory it did not create is a cleanup script that eventually
deletes somebody's work.

It finishes by running the project's checks **once** in each checkout, before any
agent is involved. A fixture whose planted bug has quietly stopped failing
produces a demo that "passes" for no reason, and the only way to know is to look.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixture"
MARKER = ".forgepilot-example"

#: Every checkout this script makes: ``(directory name, fixture, initial commit
#: subject, the variable `--export` publishes it as)``.
#:
#: The order is the order they are prepared and exported in, and the *first* entry
#: is the one the single-repository example and the eval use — `two-repos/` is the
#: only deployment that names the second. Kept as data rather than two calls so
#: that "which repositories does this example have" is answerable by reading one
#: place; a third repository is one line, and the loop in `main` needs no new
#: branch.
#:
#: The second variable is deliberately **not** in the shared `patch.yaml`: an
#: unset `${VAR}` with no default is refused, so naming it there would make a
#: second prepared checkout mandatory for the single-repository example and for
#: every eval task. `two-repos/patch.yaml` is where it is expanded.
FIXTURES: tuple[tuple[str, Path, str, str], ...] = (
    (
        "repo",
        FIXTURE,
        "metrics-stats: statistics helpers for the reporting pipeline",
        "FORGEPILOT_EXAMPLE_REPO",
    ),
    (
        "reporting",
        HERE / "fixture-reporting",
        "reporting-dashboard: rolling windows for the weekly chart",
        "FORGEPILOT_EXAMPLE_REPO_2",
    ),
)

#: Files that are ours. Anything not here must be deleted, or a stale file from an
#: older run stays in the checkout and the diff has a change nobody made.
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".git")


class Checkout(NamedTuple):
    """One prepared repository, and everything `main` needs to report about it."""

    path: Path
    fixture: Path
    message: str
    variable: str
    name: str


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


def prepare(
    repo: Path,
    *,
    recreate: bool,
    force: bool,
    fixture: Path = FIXTURE,
    message: str = "eval fixture",
) -> str:
    """Materialise one fixture checkout. Returns ``created`` or ``reused``.

    ``fixture`` is a parameter rather than a module constant read inside because
    there are two of them since M13 (see ``FIXTURES``), and the marker refusal, the
    ``-b main`` and the initial commit are the same three steps for both — a second
    copy of this function is how the two checkouts would drift apart.

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
    shutil.copytree(fixture, repo, ignore=_IGNORE)

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
        message,
        cwd=repo,
    )
    return "created"


def verify(repo: Path, *, name: str) -> bool:
    """Run the checks before any agent does. Returns True if the bug is present.

    Not a test of this script — a test of the *fixture*. A planted defect that
    stopped failing would make every later run meaningless, and it would look like
    success.

    ``name`` labels the output because there are two checkouts: "the checks exited
    non-zero" is not a useful line when the reader cannot tell which tree it was
    about, and with two of them it is exactly the question being asked.
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
    print(f"  checks   : {name}: exit {completed.returncode} (expected non-zero)")
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
    primary = root if root.name == "repo" else root / "repo"
    worktrees = (args.worktrees or primary.parent / "worktrees").resolve()

    #: Beside the first, not inside it. The second repository is a *sibling*
    #: checkout — a directory under the first one would be a file the first
    #: repository owns, and `two-repos/` delivers to these two as peers.
    checkouts = [
        Checkout(
            path=primary if index == 0 else primary.parent / name,
            fixture=fixture,
            message=message,
            variable=variable,
            name=name,
        )
        for index, (name, fixture, message, variable) in enumerate(FIXTURES)
    ]

    # `--export` never destroys; an interactive run always starts clean. See
    # `prepare` for why that asymmetry is deliberate.
    actions: list[tuple[Checkout, str]] = [
        (
            checkout,
            prepare(
                checkout.path,
                recreate=not args.export,
                force=args.force,
                fixture=checkout.fixture,
                message=checkout.message,
            ),
        )
        for checkout in checkouts
    ]

    exports = {checkout.variable: str(checkout.path) for checkout, _ in actions}
    exports["FORGEPILOT_EXAMPLE_WORKTREES"] = str(worktrees)
    # The interpreter with pytest. `project.yaml` uses this for its `test:`
    # command, and it must be the same one - a `pytest` on PATH is often not.
    exports["FORGEPILOT_PYTHON"] = sys.executable

    if args.export:
        for name, value in exports.items():
            # Single-quoted: this is shell input, and a path is not something to
            # interpolate bare.
            print(assignment(name, value, args.shell))
        return 0

    ok = True
    for checkout, action in actions:
        print(f"{action} {checkout.path}")
        ok = verify(checkout.path, name=checkout.name) and ok
    print(f"  tickets  : {HERE / 'tickets'}")
    print(f"  tickets  : {HERE / 'two-repos' / 'tickets'}")

    print()
    if not ok:
        print(
            "WARNING: the checks PASSED in a fixture that is supposed to be broken.\n"
            "         Every run after this one would be meaningless. Look at the\n"
            "         fixture named above (fixture/stats.py, or\n"
            "         fixture-reporting/reporting/windows.py).",
            file=sys.stderr,
        )

    print("run the example with:\n")
    print("  Git Bash:")
    print(
        '    eval "$(.venv/Scripts/python.exe examples/stat-fix/prepare_repo.py '
        '--export)"'
    )
    print()
    print("  PowerShell (no `eval`; `export` is not a verb there):")
    print(
        "    .venv/Scripts/python.exe examples/stat-fix/prepare_repo.py --export "
        "--shell powershell | Invoke-Expression"
    )
    print()
    print("  Then, one repository (`\\` continuation is bash-only):")
    print()
    print(
        "    .venv/Scripts/python.exe -m forgepilot --config-dir configs "
        "--patch examples/stat-fix/patch.yaml "
        "--project examples/stat-fix/project.yaml "
        "--task BUG-1 --token demo-token --allow-uncontained-exec"
    )
    print()
    print("  Or the same ticket shape across both repositories — one worktree, one")
    print("  commit and one pull request each:")
    print()
    print(
        "    .venv/Scripts/python.exe -m forgepilot --config-dir configs "
        "--patch examples/stat-fix/two-repos/patch.yaml "
        "--project examples/stat-fix/two-repos/project.yaml "
        "--task BUG-2 --token demo-token --allow-uncontained-exec"
    )
    print()
    print("(the model credential comes from .env.local - see CLAUDE.md)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
