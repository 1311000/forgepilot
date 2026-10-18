"""Build a code index for ``providers/retrieve/index.py`` from a checkout.

    python tools/build_code_index.py --repo git.corp/team/svc-a --root ../svc-a \\
        --out index.json

    # the same, at a named revision, for a mirror that is already checked out
    python tools/build_code_index.py --repo git.corp/team/svc-a --root ../svc-a \\
        --ref release/1.0 --out index.json

⚠️ **Why a builder and not hand-written JSON.** The provider's own test fixture is nine
lines, and a fixture is not a deployment: without a way to turn a real repository into an
index, the provider is only ever exercised against a corpus somebody typed. That is the
same defect as a claim with no reader — the seam exists, the demos do not.

⚠️ **The revision recorded is the one that was read, not the one requested.** The provider
serves committed revisions and every hit names its ref; an index whose `ref` was copied
from a branch name while the tree held uncommitted edits would label trunk content with a
branch name, and the caller could not tell. So the builder reads `git rev-parse HEAD` when
no `--ref` is given, and appends ``+dirty`` whenever `git status --porcelain` says the tree
is not clean — the same convention the ripgrep provider uses.

⚠️ **Only Python gets symbols.** `SymbolKind` is served from an `ast` walk, so functions,
classes and methods are indexed structurally for ``.py`` files and every other file is
text-searchable only. A regex pretending to find definitions in TypeScript would produce
hits that look structural and are not, which is exactly what `grep`'s `symbols()` refuses
to do.

⚠️ **Skipping is explicit, never silent.** Files over `--max-bytes` and files that are not
UTF-8 are left out, and the summary says how many of each. A corpus that quietly lost half
its files answers "there is nothing there" to a question it never really asked.

⚠️ **File selection comes from git, so `.gitignore` is the corpus's first filter.** A walk
of this repository produced 8 875 files and a 58 MB index — `.venv/`, `.tmp/`, pytest trees
— because a recursive glob cannot know which directories are *not* the codebase. `git
ls-files --cached --others --exclude-standard` is what the checkout actually holds: tracked
files plus untracked ones that are not ignored, which is the set an agent could have read.
A directory that is not a checkout cannot answer that question, and the builder says so
before walking it.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

FORGE = Path(__file__).resolve().parents[1]

#: A single file larger than this is almost certainly generated or vendored; indexing it
#: spends the corpus on something nobody reads. Overridable, and reported.
DEFAULT_MAX_BYTES = 512 * 1024


def _git(root: Path, *arguments: str) -> str | None:
    """One git command, or `None` when it fails. The checkout may not be a repository."""
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def is_checkout_root(root: Path) -> bool:
    """Whether `root` is the top of *its own* repository.

    ⚠️ **Not "is there a repository above it".** `git -C <dir> rev-parse HEAD` happily
    walks up, so a directory inside a monorepo answers with the whole repository's HEAD —
    and `git status` reports changes anywhere in it. An index of a subdirectory labelled
    with the repository's revision would claim to hold the branch it holds a corner of.
    """
    top = _git(root, "rev-parse", "--show-toplevel")
    return top is not None and Path(top).resolve() == root.resolve()


def revision(root: Path, requested: str | None) -> str:
    """The revision that will be recorded, dirt included.

    Read rather than echoed, for the reason in the module docstring: an index labelled
    with a branch name while the content came from a dirty tree is a lie about a
    revision, and every hit would repeat it.
    """
    if not is_checkout_root(root):
        if not requested:
            raise SystemExit(
                f"{root} is not the top of a git checkout, so there is no revision to "
                f"record. Pass --ref to name one yourself."
            )
        print(
            f"note: {root} is not the top of a git checkout; recording --ref "
            f"{requested!r} as given, with no dirty check",
            file=sys.stderr,
        )
        return requested

    base = requested or _git(root, "rev-parse", "HEAD")
    if not base:
        raise SystemExit(f"{root} has no commits, so there is no revision to record.")
    dirty = _git(root, "status", "--porcelain")
    return f"{base}+dirty" if dirty else base


def symbols_for(text: str, path: str, ref: str) -> list[dict]:
    """Structural symbols in one Python file, with the spans a reader can be handed.

    A file that does not parse contributes no symbols and is still searchable text: the
    corpus is allowed to contain broken code, because a task about a broken file is a
    normal thing to ask.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    found: list[dict] = []

    def entry(node: ast.AST, name: str, kind: str) -> dict:
        end = getattr(node, "end_lineno", None) or getattr(node, "lineno", 1)
        segment = ast.get_source_segment(text, node) or ""
        return {
            "repo": None,  # filled in by build(), which knows the slug
            "path": path,
            "ref": ref,
            "name": name,
            "kind": kind,
            "start": getattr(node, "lineno", 1),
            "end": end,
            "signature": segment.splitlines()[0].strip() if segment else None,
        }

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            found.append(entry(node, node.name, "class"))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.append(entry(child, child.name, "method"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(entry(node, node.name, "function"))

    return found


def _tracked_paths(root: Path) -> list[str] | None:
    """What the checkout holds, according to git — ignored files excluded.

    ⚠️ **Not `rglob` when git can answer.** A first version walked the tree, and on this
    repository that meant 8 875 files and a 58 MB index: `.venv/`, `.tmp/` and every pytest
    tree were indexed as if they were the codebase. A corpus that is mostly installed
    packages answers every query with something, and none of it is the repository. `git
    ls-files --cached --others --exclude-standard` is the honest enumeration: tracked files
    plus untracked-and-not-ignored ones, which is exactly the content an agent could have
    read.
    """
    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return None
    return [entry for entry in completed.stdout.split("\0") if entry]


def build(
    *,
    repo: str,
    root: Path,
    ref: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[dict, dict[str, int]]:
    """The index payload and what was skipped while making it."""
    files: list[dict] = []
    symbols: list[dict] = []
    skipped: dict[str, int] = {"large": 0, "binary": 0}

    tracked = _tracked_paths(root) if is_checkout_root(root) else None
    if tracked is None:
        # Not a checkout: there is no ignore list to consult, so say so rather than
        # pretending the walk was a repository.
        print(
            f"note: {root} is not a git checkout, so .gitignore is not applied and every "
            f"file under it is a candidate",
            file=sys.stderr,
        )
        candidates = [
            candidate
            for candidate in sorted(root.rglob("*"))
            if candidate.is_file() and ".git" not in candidate.relative_to(root).parts
        ]
    else:
        candidates = [root / entry for entry in tracked]

    for candidate in candidates:
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root).as_posix()
        try:
            if candidate.stat().st_size > max_bytes:
                skipped["large"] += 1
                continue
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Binary or unreadable: not searchable text, and saying so once is better
            # than indexing mojibake that no query will ever match correctly.
            skipped["binary"] += 1
            continue

        files.append({"repo": repo, "path": relative, "ref": ref, "text": text})
        if candidate.suffix == ".py":
            for symbol in symbols_for(text, relative, ref):
                symbol["repo"] = repo
                symbols.append(symbol)

    payload = {"files": files, "symbols": symbols}
    return payload, skipped


def main(argv: list[str] | None = None) -> int:
    from forgepilot.console import use_utf8_output

    use_utf8_output()

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="host/namespace/name, as a RepoRef slug")
    parser.add_argument("--root", required=True, type=Path, help="the checkout to index")
    parser.add_argument("--out", required=True, type=Path, help="where to write the JSON index")
    parser.add_argument(
        "--ref",
        default=None,
        help="revision to record; default is the checkout's HEAD, and every dirty tree is "
        "recorded as `<revision>+dirty`",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"skip files larger than this (default {DEFAULT_MAX_BYTES})",
    )
    args = parser.parse_args(argv)

    parts = args.repo.split("/")
    if len(parts) != 3 or not all(parts):
        raise SystemExit(
            f"--repo {args.repo!r} is not <host>/<namespace>/<name>; the provider would "
            f"refuse the entry, so refusing it here names the flag instead of the file."
        )

    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"--root {root} is not a directory")

    ref = revision(root, args.ref)
    payload, skipped = build(repo=args.repo, root=root, ref=ref, max_bytes=args.max_bytes)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    print(
        f"{len(payload['files'])} file(s), {len(payload['symbols'])} symbol(s) "
        f"at {ref} -> {args.out}"
    )
    if skipped["large"] or skipped["binary"]:
        print(
            f"skipped: {skipped['large']} file(s) over {args.max_bytes} bytes, "
            f"{skipped['binary']} not UTF-8 text",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
