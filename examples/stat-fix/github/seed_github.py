"""Put the stat-fix fixture into a GitHub repository.

`prepare_repo.py` makes a *local* checkout out of `../fixture`; this makes a
*remote* one. Same source, two destinations — because the point of the GitHub
demo is that the whole delivery loop runs against a real repo host, and that
needs a repository on the host to run against.

The credential comes from the environment and is handled by the provider's own
`authenticated_env`, not by a second copy of that logic here. A second copy would
be wrong in a *different* way from the provider, and the one that got it right
would be the one nobody ran.

    $ export FORGEPILOT_GITHUB_TOKEN=...        # `repo` scope; never a file
    $ .venv/Scripts/python.exe examples/stat-fix/github/seed_github.py

Then follow the printed instructions. Nothing here destroys anything: a seed
checkout this script made earlier is reused, and the push is a fast-forward.

Two flags exist for the **two-repository** deployment, where the second fixture
is a different directory and the second repository may not exist yet:

    --fixture examples/stat-fix/fixture-reporting   seed a different fixture
    --create                                        make the repository first

`--create` is the operator action the script exists to avoid doing by hand. It
goes through `create_repository` beside the provider's token handling rather than
a second copy of "how the credential is sent", and a repository that is already
there is the answer, not an error.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent / "fixture"
MARKER = ".forgepilot-example"


def _git(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    secrets: tuple[str, ...] = (),
) -> str:
    """Run git, and make sure nothing it says can carry the credential out.

    Redacted for the same reason the provider redacts: git quotes the remote URL
    in its failures, and this script's failures are shown to a person.
    """
    from forgepilot.providers.vcs.github import redact

    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = redact(completed.stderr or completed.stdout or "", secrets)
        raise SystemExit(f"git {' '.join(args)} failed:\n{detail.strip()}")
    return completed.stdout


def _has_staged_changes(cwd: Path) -> bool:
    """Whether the index differs from HEAD.

    A seed built on top of an existing ``main`` whose content is already the
    fixture has **nothing to commit**, and `git commit` exits non-zero saying so.
    Refusing to continue there would be wrong twice: the repository ends up with
    the right content, and the message reads as a failure. `--quiet` makes the
    exit code the answer (1 = differences, 0 = none).
    """
    completed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.returncode != 0


def assignment(name: str, value: str, shell: str) -> str:
    """One line that sets ``name`` in ``shell``.

    The two shells are not interchangeable, and pretending otherwise is how
    somebody ends up pasting `eval "$(...)"` into PowerShell and being told
    `eval` is not recognised. `export` is not a PowerShell verb either, so the
    output has to differ rather than merely be wrapped differently.
    """
    quoted = value.replace("'", "''")  # both shells escape a quote by doubling it
    if shell == "powershell":
        return f"$env:{name} = '{quoted}'"
    return f"export {name}='{quoted}'"


def seed(
    token: str,
    slug: str,
    into: Path,
    *,
    force: bool,
    remote_base: str = "",
    fixture: Path = FIXTURE,
) -> None:
    from forgepilot.providers.vcs.github import authenticated_env, remote_url
    from forgepilot.seams.refs import RepoRef

    host, namespace, name = slug.split("/")
    repo = RepoRef(host=host, namespace=namespace, name=name)
    # `remote_base` is how you rehearse this without a token or a network: point
    # it at a local bare repository and every step below runs for real — init,
    # commit, remote, push — with only the URL differing. Verifying the sequence
    # is worth more than verifying the URL, and the URL is one string.
    base = remote_base or f"https://{host}"
    url = remote_url(base, repo)
    env = authenticated_env(token, base)

    from forgepilot.fs import remove_tree

    if into.exists():
        if not (into / MARKER).is_file() and not force:
            raise SystemExit(
                f"refusing to touch {into}: it has no {MARKER} marker, so it is not a "
                f"checkout this script made. Pass --into elsewhere, or --force."
            )
        if force:
            remove_tree(into)

    if not into.exists():
        into.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            fixture,
            into,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
        _git("init", "-b", "main", cwd=into)
        _git("add", "-A", cwd=into)

        # If the host already has a `main`, build the fixture commit **on top of
        # it** rather than starting a second root commit. Two unrelated histories
        # make the push a non-fast-forward, and the only ways out of that are a
        # force-push to somebody's repository or a manual pull that drops the
        # fixture's content. `--soft` moves HEAD without touching the index, so
        # the files just copied are exactly what the commit records.
        existing = _git(
            "ls-remote",
            "--heads",
            url,
            "refs/heads/main",
            cwd=into,
            env=env,
            secrets=(token,),
        ).strip()
        if existing:
            _git(
                "fetch",
                "--quiet",
                url,
                "refs/heads/main",
                cwd=into,
                env=env,
                secrets=(token,),
            )
            _git("reset", "--soft", "FETCH_HEAD", cwd=into)

        if _has_staged_changes(into):
            _git(
                "-c",
                "user.name=ForgePilot Example",
                "-c",
                "user.email=example@localhost",
                "commit",
                "-m",
                f"{repo.name}: the fixture this example runs against",
                cwd=into,
            )
        # The *plain* URL is what gets recorded. See the provider's module
        # docstring: a token-URL here would be a credential at rest in this
        # directory, and it would travel with any copy of it.
        _git("remote", "add", "origin", url, cwd=into)

    # `main` to `main`, named explicitly. The remote may be a repository with no
    # commits at all, where `git push origin` has no upstream to reason about and
    # falls back to the local branch name — an assumption that breaks on any
    # machine whose `init.defaultBranch` is not `main`.
    _git(
        "push",
        url,
        "refs/heads/main:refs/heads/main",
        cwd=into,
        env=env,
        secrets=(token,),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--slug",
        default="github.com/1311000/test",
        help="host/namespace/name of the target repository",
    )
    parser.add_argument(
        "--into",
        type=Path,
        default=None,
        help="where to build the seed checkout (default: under FORGEPILOT_HOME)",
    )
    parser.add_argument("--force", action="store_true", help="recreate the seed checkout")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=FIXTURE,
        help=(
            "the directory to seed from (default: ../fixture, the statistics "
            "fixture). The two-repository deployment seeds its second repository "
            "from ../fixture-reporting."
        ),
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help=(
            "create the repository on the host first. A repository that is already "
            "there is not an error - the question is whether one exists, not whether "
            "this call is what made it."
        ),
    )
    parser.add_argument(
        "--remote-base",
        default="",
        help=(
            "override the https base the clone URL is built from. Point it at a "
            "local bare repository to rehearse the whole flow without a token or a "
            "network - only the URL differs."
        ),
    )
    parser.add_argument("--export", action="store_true", help="print only `export` lines")
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

    home = os.environ.get("FORGEPILOT_HOME")
    base = Path(home) if home else Path.home() / ".forgepilot"
    example = base / "examples" / "stat-fix"
    name = args.slug.rsplit("/", 1)[-1]
    # Per repository rather than one shared directory: the second repository's
    # seed checkout would otherwise be the first one's, and `--force` on the
    # second would silently rewrite the first repository's history.
    into = (args.into or example / f"github-seed-{name}").resolve()

    exports = {
        "FORGEPILOT_EXAMPLE_MIRRORS": str(example / "mirrors"),
        "FORGEPILOT_EXAMPLE_WORKTREES": str(example / "github-worktrees"),
        "FORGEPILOT_PYTHON": sys.executable,
    }

    if args.export:
        # **`--export` prints paths and does nothing else.** It is the thing that
        # goes inside `eval "$(...)"`, and a command whose output is shell
        # assignments must not also be doing network work. This is not
        # hypothetical: `git push` against github.com failed once and succeeded
        # the next time on the same machine. A push on this path means that when
        # the link is down the substitution yields *nothing*, `eval ""` leaves the
        # caller with no variables at all, and the next command reports
        # `FORGEPILOT_EXAMPLE_MIRRORS is not set` — an error about the wrong thing
        # entirely, pointing at a file that is fine.
        #
        # So this is deliberately not `prepare_repo.py`'s rule ("`--export` must
        # not destroy") but a stronger one: the env-var producer must not depend
        # on the network. Seeding is what the plain run is for, and it prints the
        # line below for you to copy.
        for key, value in exports.items():
            print(assignment(key, value, args.shell))
        return 0

    # Load the gitignored .env.local the same way the CLI does, so the token does
    # not have to be exported in every shell. The real environment still wins.
    from forgepilot.env import load_local_env

    load_local_env(HERE.parent.parent)

    token = os.environ.get("FORGEPILOT_GITHUB_TOKEN", "")
    if not token:
        print(
            "error: FORGEPILOT_GITHUB_TOKEN is unset.\n"
            "       A token with `repo` scope is needed to push. Put it in the\n"
            "       gitignored .env.local at the repository root, or export it -\n"
            "       never in a file that gets committed.",
            file=sys.stderr,
        )
        return 2

    if args.slug.count("/") != 2:
        print(f"error: --slug must be 'host/namespace/name', got {args.slug!r}", file=sys.stderr)
        return 2

    host, namespace, repo_name = args.slug.split("/")
    if args.create:
        from forgepilot.providers.vcs.github import create_repository
        from forgepilot.seams.refs import RepoRef

        repo = RepoRef(host=host, namespace=namespace, name=repo_name)
        made = create_repository(token, repo)
        print(("created " if made else "already there: ") + args.slug)

    print(f"seeding {args.slug} from {args.fixture}")
    seed(
        token,
        args.slug,
        into,
        force=args.force,
        remote_base=args.remote_base,
        fixture=args.fixture,
    )

    seeder = "examples/stat-fix/github/seed_github.py"
    run = (
        ".venv/Scripts/python.exe -m forgepilot --config-dir configs "
        "--patch examples/stat-fix/github/patch.yaml "
        "--project examples/stat-fix/github/project.yaml "
        "--task BUG-1 --token demo-token --allow-uncontained-exec"
    )

    print(f"pushed main to {args.slug}")
    print()
    print("Two steps. First, the three variables the patch needs.")
    print()
    print("  Git Bash:")
    print(f'    eval "$(.venv/Scripts/python.exe {seeder} --export)"')
    print()
    print("  PowerShell (no `eval`; `export` is not a verb there):")
    print(f"    .venv/Scripts/python.exe {seeder} --export --shell powershell | Invoke-Expression")
    print()
    print("Then the task itself (one line; `\\` continuation is bash-only):")
    print()
    print(f"    {run}")
    print()
    print("(the model credential comes from .env.local - see CLAUDE.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
