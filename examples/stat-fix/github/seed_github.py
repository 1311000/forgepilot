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


def _force_remove(path: Path) -> None:
    """`rmtree` that survives the read-only object files git leaves behind.

    The same defect `prepare_repo.py` hit: git marks its objects read-only, and
    Windows raises `PermissionError` rather than clearing the attribute.
    """
    import stat

    def onexc(func, target, exc):  # type: ignore[no-untyped-def]
        if not isinstance(exc, PermissionError):
            raise exc
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=onexc)


def seed(token: str, slug: str, into: Path, *, force: bool) -> None:
    from forgepilot.providers.vcs.github import authenticated_env, remote_url
    from forgepilot.seams.refs import RepoRef

    host, namespace, name = slug.split("/")
    repo = RepoRef(host=host, namespace=namespace, name=name)
    base = f"https://{host}"
    url = remote_url(base, repo)
    env = authenticated_env(token, base)

    if into.exists():
        if not (into / MARKER).is_file() and not force:
            raise SystemExit(
                f"refusing to touch {into}: it has no {MARKER} marker, so it is not a "
                f"checkout this script made. Pass --into elsewhere, or --force."
            )
        if force:
            _force_remove(into)

    if not into.exists():
        into.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            FIXTURE,
            into,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
        _git("init", "-b", "main", cwd=into)
        _git("add", "-A", cwd=into)
        _git(
            "-c",
            "user.name=ForgePilot Example",
            "-c",
            "user.email=example@localhost",
            "commit",
            "-m",
            "metrics-stats: statistics helpers for the reporting pipeline",
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
    parser.add_argument("--export", action="store_true", help="print only `export` lines")
    args = parser.parse_args(argv)

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

    home = os.environ.get("FORGEPILOT_HOME")
    base = Path(home) if home else Path.home() / ".forgepilot"
    example = base / "examples" / "stat-fix"
    into = (args.into or example / "github-seed").resolve()

    if not args.export:
        print(f"seeding {args.slug} from {FIXTURE}")
    seed(token, args.slug, into, force=args.force)

    exports = {
        "FORGEPILOT_EXAMPLE_MIRRORS": str(example / "mirrors"),
        "FORGEPILOT_EXAMPLE_WORKTREES": str(example / "github-worktrees"),
        "FORGEPILOT_PYTHON": sys.executable,
    }
    if args.export:
        for key, value in exports.items():
            print(f"export {key}='{value}'")
        return 0

    print(f"pushed main to {args.slug}")
    print()
    print("run the GitHub example with:\n")
    print(
        '  eval "$(.venv/Scripts/python.exe examples/stat-fix/github/seed_github.py --export)"'
    )
    print()
    print("  .venv/Scripts/python.exe -m forgepilot --config-dir configs \\")
    print("      --patch   examples/stat-fix/github/patch.yaml \\")
    print("      --project examples/stat-fix/github/project.yaml \\")
    print("      --task BUG-1 --token demo-token --allow-uncontained-exec")
    print()
    print("(the model credential comes from .env.local - see CLAUDE.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
