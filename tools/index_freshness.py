"""Publish how far the index is behind each checkout, in a form Prometheus can scrape.

`lag()` has had readers since P25 — `--check` and the two retrieval tools — but nothing
ran it *on a schedule*, and a freshness check nobody runs is a field nobody reads. This is
the missing half: a process whose whole job is to compare the index's per-repository refs
with the checkouts' HEADs and print the difference as metrics.

Why an exporter rather than a gauge inside the service: the index does not have the
checkouts. Only the machine that holds them knows what `HEAD` is, so the honest place to
compute staleness is beside the repositories, not beside the corpus. The server publishes
`refsByRepo`; this reads it.

Exit codes, because a cron job needs to fail: 0 fresh, 1 stale (or older than the age
limit), 2 refused/unreachable/misconfigured. Prometheus text goes to stdout; the human
summary goes to stderr, so a scrape is never polluted by prose.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from forgepilot.providers.retrieve.remote import HttpIndexService  # noqa: E402
from forgepilot.seams.index_service import IndexStatus, lag  # noqa: E402
from forgepilot.seams.refs import RepoRef  # noqa: E402
from forgepilot.seams.security import ExecutionConstraints  # noqa: E402

#: How old the index may be before the age series crosses the alert threshold. The alert
#: carries its own `for:`; this is the value the *tool* fails on, so a cron job and an
#: alert disagreeing about "too old" is caught by whoever reads both.
DEFAULT_MAX_AGE_HOURS = 24.0

__all__ = ["Measurement", "measure", "parse_repo", "render"]


@dataclass(frozen=True, slots=True)
class Measurement:
    repo: str
    head: str
    indexed_ref: str
    behind_commits: int
    known: bool


def parse_repo(value: str) -> tuple[RepoRef, Path]:
    """`host/namespace/name=PATH`, the same shape the other index tools take."""

    slug, _, path = value.partition("=")
    parts = slug.strip().split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"a repository is host/namespace/name, not {slug!r}")
    if not path.strip():
        raise ValueError(f"{slug!r} needs a checkout path after '='")
    return RepoRef(host=parts[0], namespace=parts[1], name=parts[2]), Path(path.strip())


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def head_of(root: Path) -> str:
    """The checkout's `HEAD`, and only that checkout's.

    `git -C <plain dir> rev-parse HEAD` walks up to an enclosing repository, which on this
    project's own tree means a directory that is not a checkout answers with somebody
    else's HEAD. The fourth time this trap has cost a wrong answer.
    """

    top = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root.resolve():
        raise RuntimeError(f"{root} is not the root of a git checkout")
    return _git(root, "rev-parse", "HEAD")


def behind(root: Path, indexed_ref: str, head: str) -> int:
    """How many commits the index is behind, or a reason it cannot be counted.

    A ref the checkout has never heard of is **not** zero: it means the index holds
    something this repository cannot show, so it is reported as `1` with the ref named
    rather than quietly counted as fresh.
    """

    if indexed_ref == head:
        return 0
    try:
        count = int(_git(root, "rev-list", "--count", f"{indexed_ref}..{head}"))
    except (RuntimeError, ValueError):
        return 1
    return max(count, 0)


def measure(
    status: IndexStatus,
    checkouts: Mapping[RepoRef, Path],
    *,
    token: str = "",
) -> list[Measurement]:
    """One measurement per checkout, using the seam's rule for what "unknown" means."""

    measured: list[Measurement] = []
    for repo, root in checkouts.items():
        slug = repo.slug
        try:
            head = head_of(root)
        except RuntimeError:
            measured.append(
                Measurement(slug, "", status.refs_by_repo.get(slug, ""), 1, known=False)
            )
            continue
        answer = lag(status, repo, head)
        if not answer.behind:
            measured.append(Measurement(slug, head, answer.indexed_ref, 0, known=True))
            continue
        measured.append(
            Measurement(
                slug,
                head,
                answer.indexed_ref,
                behind(root, answer.indexed_ref, head) if answer.indexed_ref else 1,
                known=bool(answer.indexed_ref),
            )
        )
    return measured


def age_seconds(updated_at: str, *, now: datetime | None = None) -> float:
    """Seconds since the index was last updated; 0 when it did not say.

    Zero rather than infinity: a service that does not report a timestamp is a gap in the
    exporter's knowledge, not evidence of an ancient corpus, and an alert firing on a
    missing field trains people to ignore it. `lag()` is where "unknown means stale" lives,
    and it is applied per repository.
    """

    if not updated_at:
        return 0.0
    try:
        stamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max((now or datetime.now(UTC)) - stamp, timedelta()).total_seconds()


def render(
    measurements: Sequence[Measurement],
    *,
    age: float,
    indexed_refs: Sequence[str] = (),
) -> str:
    """Prometheus text exposition. No labels that vary per scrape, and no timestamps."""

    lines = [
        "# HELP forgepilot_index_lag_commits Commits between the index's ref and the checkout's HEAD.",
        "# TYPE forgepilot_index_lag_commits gauge",
    ]
    for item in measurements:
        ref = item.indexed_ref or "unknown"
        lines.append(
            f'forgepilot_index_lag_commits{{repo="{item.repo}",indexed="{ref}"}} {item.behind_commits}'
        )
    lines.extend(
        [
            "# HELP forgepilot_index_age_seconds Seconds since the index service last updated.",
            "# TYPE forgepilot_index_age_seconds gauge",
            f"forgepilot_index_age_seconds {age:.0f}",
            "# HELP forgepilot_index_refs Number of per-repository refs the service reports.",
            "# TYPE forgepilot_index_refs gauge",
            f"forgepilot_index_refs {len(indexed_refs)}",
        ]
    )
    return "\n".join(lines) + "\n"


def _constraints(checkouts: Mapping[RepoRef, Path]) -> ExecutionConstraints:
    return ExecutionConstraints(
        principal_id="index-freshness",
        tenant_id="default",
        allowed_repos=frozenset(checkouts),
    )


async def run(args: argparse.Namespace) -> int:
    if not args.index_url:
        print("error: --index-url is required", file=sys.stderr)
        return 2
    try:
        checkouts = dict(parse_repo(value) for value in args.repo)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not checkouts:
        print("error: at least one --repo host/namespace/name=PATH is required", file=sys.stderr)
        return 2

    token = os.environ.get(args.index_token_env or "", "")
    service = HttpIndexService(args.index_url, token=token, timeout_s=args.timeout)
    try:
        status = await service.status(_constraints(checkouts))
    except Exception as exc:  # noqa: BLE001 - every failure is an exit code
        print(f"error: the index did not answer: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    measurements = measure(status, checkouts, token=token)
    age = age_seconds(status.updated_at)
    if args.json:
        print(
            json.dumps(
                {
                    "ageSeconds": age,
                    "repositories": [
                        {
                            "repo": item.repo,
                            "head": item.head,
                            "indexedRef": item.indexed_ref,
                            "lagCommits": item.behind_commits,
                        }
                        for item in measurements
                    ],
                }
            )
        )
    else:
        sys.stdout.write(render(measurements, age=age, indexed_refs=tuple(status.refs_by_repo)))

    stale = [item for item in measurements if item.behind_commits > args.max_lag]
    for item in measurements:
        state = "fresh" if item.behind_commits == 0 else f"{item.behind_commits} commit(s) behind"
        print(f"{item.repo}: {state}", file=sys.stderr)
    if age > args.max_age_hours * 3600:
        print(f"the index was last updated {age / 3600:.1f}h ago", file=sys.stderr)
        return 1
    if stale:
        print(
            f"{len(stale)} repository/repositories past the {args.max_lag}-commit limit",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-url", default="", help="base URL of the index service")
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="HOST/NAMESPACE/NAME=PATH",
        help="a repository and the checkout whose HEAD to compare (repeatable)",
    )
    parser.add_argument(
        "--index-token-env",
        default="FORGEPILOT_INDEX_TOKEN",
        help="environment variable holding the read token",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-lag", type=int, default=0, help="commits tolerated before exit 1")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument(
        "--json", action="store_true", help="print one JSON object instead of metrics"
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
