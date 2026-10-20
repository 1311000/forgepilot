"""Download SWE-bench-lite instances into the JSONL the spike reads.

The spike refuses to fetch its own questions — a benchmark that downloads them at
run time can change them between two runs. This is the separate, explicit step: it
writes a file, then **reads it back through the adapter** and refuses to leave an
instance file the spike could not measure, so "the download worked" and "the file is
usable" cannot be two different answers.

Fields are narrowed to the ones `evals/swebench.py` reads. `FAIL_TO_PASS` and
`PASS_TO_PASS` are copied verbatim (the dataset stores them as JSON strings), because
rewriting them here would put a second interpretation of the dataset in the tree.

Usage, behind a proxy when the direct route is blocked:

    python tools/swebench_fetch.py --limit 3 --out .tmp/swebench_lite.jsonl \\
        --proxy http://127.0.0.1:18787
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evals.swebench import MalformedInstance, load_instances  # noqa: E402

DEFAULT_API = "https://datasets-server.huggingface.co"
DEFAULT_DATASET = "princeton-nlp/SWE-bench_Lite"
KEPT = (
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "test_patch",
    "patch",
)


def _opener(proxy: str | None) -> urllib.request.OpenerDirector:
    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # An inherited proxy variable is how "it worked on my machine" gets into a
        # benchmark: name the route explicitly or take the direct one.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _rows(
    opener: urllib.request.OpenerDirector,
    api: str,
    dataset: str,
    config: str,
    split: str,
    *,
    offset: int,
    length: int,
    timeout: float,
) -> tuple[list[dict[str, Any]], int]:
    query = urllib.parse.urlencode(
        {
            "dataset": dataset,
            "config": config,
            "split": split,
            "offset": offset,
            "length": length,
        }
    )
    url = f"{api.rstrip('/')}/rows?{query}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"error: {url} answered {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"error: cannot reach {api} ({exc.reason}). This network has no route to the "
            "HuggingFace dataset servers unless a proxy is given; --proxy names one."
        ) from exc
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise SystemExit(f"error: {url} answered without a 'rows' list")
    total = payload.get("num_rows_total")
    total = total if isinstance(total, int) else offset + len(rows)
    narrowed: list[dict[str, Any]] = []
    for entry in rows:
        row = entry.get("row") if isinstance(entry, dict) else None
        if not isinstance(row, dict):
            raise SystemExit(f"error: {url} returned a row that is not an object")
        narrowed.append({key: row[key] for key in KEPT if key in row})
    return narrowed, total


def fetch(
    *,
    out: Path,
    limit: int,
    api: str = DEFAULT_API,
    dataset: str = DEFAULT_DATASET,
    config: str = "default",
    split: str = "test",
    proxy: str | None = None,
    page: int = 100,
    timeout: float = 30.0,
    repo: str | None = None,
) -> tuple[str, ...]:
    """Write at most `limit` instances to `out`, then prove the file is usable.

    `repo` keeps only rows whose `repo` contains it. The split is ordered by repository,
    so without a filter the first `n` rows are the first `n` of one repository — which is
    fine for a spike and useless for "one instance of a repository that installs".
    """
    if limit < 1:
        raise ValueError(f"limit must be positive, not {limit}")
    opener = _opener(proxy)
    kept: list[dict[str, Any]] = []
    offset = 0
    while len(kept) < limit:
        # With a filter the page size cannot be the number still wanted: most rows in a
        # page will not match, and asking for `limit - len(kept)` rows per request would
        # walk the split one row at a time.
        length = page if repo else min(page, limit - len(kept))
        rows, total = _rows(
            opener,
            api,
            dataset,
            config,
            split,
            offset=offset,
            length=length,
            timeout=timeout,
        )
        offset += len(rows)
        for row in rows:
            if repo and repo not in str(row.get("repo", "")):
                continue
            kept.append(row)
            if len(kept) >= limit:
                break
        if not rows or offset >= total:
            break

    if not kept:
        wanted = f" matching {repo!r}" if repo else ""
        raise SystemExit(f"error: {dataset} ({config}/{split}) returned no rows{wanted}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept),
        encoding="utf-8",
    )

    # The round trip is the check: a file this refuses is a file the spike would
    # have refused later, with less context about where it came from.
    try:
        instances = load_instances(out, limit=limit)
    except MalformedInstance as exc:
        raise SystemExit(
            f"error: {out} was written but the adapter refuses it: {exc}. "
            "The dataset shape changed; do not pass this file to the spike."
        ) from exc
    return tuple(instance.instance_id for instance in instances)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="where to write the JSONL")
    parser.add_argument("--limit", type=int, default=3, help="how many instances (default 3)")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--config", default="default")
    parser.add_argument("--split", default="test")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument(
        "--proxy",
        default=None,
        help="http(s) proxy for the dataset server, e.g. http://127.0.0.1:18787",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--page",
        type=int,
        default=100,
        help=(
            "rows per request (default 100). A proxy that truncates a large body shows up "
            "as http.client.IncompleteRead; a smaller page is the workaround, and it is a "
            "flag rather than a retry because the truncation here is size-dependent"
        ),
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="keep only instances of this repository (substring, e.g. psf/requests)",
    )
    args = parser.parse_args(argv)

    try:
        ids = fetch(
            out=args.out,
            limit=args.limit,
            api=args.api,
            dataset=args.dataset,
            config=args.config,
            split=args.split,
            proxy=args.proxy,
            timeout=args.timeout,
            repo=args.repo,
            page=args.page,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"wrote {len(ids)} instance(s) to {args.out}")
    for instance_id in ids:
        print(f"  {instance_id}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the module is the test surface
    raise SystemExit(main())
