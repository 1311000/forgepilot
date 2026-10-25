"""How long a sharded index takes to answer, on a corpus big enough to have an opinion.

The question this answers is narrow and the answer is only useful with the corpus size
beside it: *with 10 000 files, what is the p95 of one query, and what does splitting the
corpus into shards cost or save?* Both layouts run on the same machine, the same process
count and the same file set, so the difference is the layout and nothing else.

It is not a claim about an enterprise deployment: one host, one process, an in-memory FTS5
index, no network between shards. It is the measurement that keeps the design honest —
a fan-out that costs more than the scan it replaces is a fan-out that should not exist.

    .venv/Scripts/python.exe tools/bench_index.py --files 10000 --queries 60
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

FORGE = Path(__file__).resolve().parents[1]
SERVE = FORGE / "tools" / "serve_index.py"
TOKEN = "bench-token"

WORDS = (
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi "
    "rho sigma tau upsilon phi chi psi omega page window clamp guard handler router"
).split()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _corpus_text(seed: int) -> str:
    rng = random.Random(seed)
    lines = [f"# module {seed}", ""]
    for index in range(3):
        words = " ".join(rng.choice(WORDS) for _ in range(6))
        lines.append(f"def function_{seed}_{index}(requested: int) -> int:")
        lines.append(f"    # {words}")
        lines.append(f"    return clamp(requested, {seed % 100})")
        lines.append("")
    return "\n".join(lines)


def build_corpus(path: Path, *, files: int, repos: int) -> dict[str, Any]:
    """A synthetic corpus with the shape that matters: several repositories, each big.

    The needle is one file per repository, so a query that hits is a query that has to
    look, and a query that misses is not a special case the implementation can shortcut.
    """
    per_repo = max(1, files // repos)
    payload: dict[str, Any] = {"files": [], "symbols": []}
    for repo_index in range(repos):
        slug = f"git.corp/team/repo-{repo_index}"
        for file_index in range(per_repo):
            bucket = file_index % 10
            where = f"mod{bucket:02d}/file_{file_index:04d}.py"
            payload["files"].append(
                {
                    "repo": slug,
                    "path": where,
                    "ref": "main",
                    "text": _corpus_text(repo_index * per_repo + file_index),
                }
            )
            payload["symbols"].append(
                {
                    "repo": slug,
                    "path": where,
                    "ref": "main",
                    "name": f"function_{repo_index * per_repo + file_index}_0",
                    "kind": "function",
                    "start": 3,
                    "end": 5,
                }
            )
        # One needle per bucket, so a narrowed query still has hits to return: a
        # benchmark whose selective case answers nothing measures the cost of returning
        # nothing, which is not the question.
        for bucket in range(10):
            payload["files"].append(
                {
                    "repo": slug,
                    "path": f"mod{bucket:02d}/NEEDS_REVIEW.py",
                    "ref": "main",
                    "text": f"# needs-review-{repo_index}-{bucket}\nVALUE = {repo_index}\n",
                }
            )
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _start(index: Path, *, layout: str) -> tuple[subprocess.Popen[bytes], str]:
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(SERVE),
            "--index",
            str(index),
            "--port",
            str(port),
            "--token",
            f"{TOKEN}=*",
            "--shard-by",
            layout,
        ],
        cwd=str(FORGE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the service exited with {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return process, base
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("the service never accepted a connection")


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _one_shape(
    client: httpx.Client, *, slugs: list[str], queries: int, globs: list[str] | None
) -> dict[str, Any]:
    latencies: list[float] = []
    hits = 0
    payload: dict[str, Any] = {"pattern": "needs-review", "repos": slugs, "ref": "main"}
    if globs:
        payload["globs"] = globs
    for _ in range(queries):
        started = time.perf_counter()
        body = client.post("/search", json=payload).json()
        latencies.append((time.perf_counter() - started) * 1000.0)
        hits += len(body.get("hits", ()))
    return {
        "hits": hits,
        "p50_ms": round(_percentile(latencies, 0.50), 2),
        "p95_ms": round(_percentile(latencies, 0.95), 2),
        "max_ms": round(max(latencies), 2),
    }


def measure(base: str, *, repos: int, queries: int) -> dict[str, Any]:
    """Two shapes, because they answer different questions.

    ``all`` asks every shard — the fan-out's worst case, and where a layout with more
    shards pays for the fan-out. ``narrow`` names one directory, which is the case a
    sharded index exists for: the shards that cannot hold a match are never asked.
    """
    headers = {"Authorization": f"Bearer {TOKEN}"}
    slugs = [f"git.corp/team/repo-{index}" for index in range(repos)]
    with httpx.Client(base_url=base, headers=headers, timeout=60.0) as client:
        status = client.get("/status").json()
        shards = len(status.get("shards", ()))
        # Warm the process: the first query pays for lazy imports, not for the corpus.
        client.post("/search", json={"pattern": "clamp", "repos": [slugs[0]], "ref": "main"})
        all_shards = _one_shape(client, slugs=slugs, queries=queries, globs=None)
        narrowed = _one_shape(client, slugs=slugs, queries=queries, globs=["mod03/**"])
    return {"shards": shards, "all": all_shards, "narrow": narrowed}


# --- a real checkout, because a synthetic corpus cannot answer a recall question ---------
#
# The synthetic corpus above measures latency at a size somebody chose. It cannot answer
# "does the index find the definition", because its needles were planted: every query has a
# known answer that was written next to it. A real checkout has real names, real collisions
# (`__init__`, `test_...`, `setup`) and symbols whose defining line the index has to have
# parsed correctly — so it is the only corpus on which a recall number means anything.

MAX_FILE_BYTES = 512 * 1024


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=True, timeout=120
    )
    return done.stdout.decode("utf-8", "replace")


def _tracked(root: Path) -> list[str]:
    raw = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def build_real_corpus(
    targets: list[tuple[str, Path]], path: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Index real checkouts. Returns the payload and the symbol rows that were indexed."""
    from forgepilot.providers.retrieve.index import symbols_for

    payload: dict[str, Any] = {"files": [], "symbols": []}
    skipped = {"binary": 0, "large": 0}
    for slug, root in targets:
        ref = _git(root, "rev-parse", "HEAD").strip()
        if _git(root, "status", "--porcelain").strip():
            ref = f"{ref}+dirty"
        for relative in _tracked(root):
            source = root / relative
            if not source.is_file():
                continue
            if source.stat().st_size > MAX_FILE_BYTES:
                skipped["large"] += 1
                continue
            try:
                text = source.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                skipped["binary"] += 1
                continue
            payload["files"].append({"repo": slug, "path": relative, "ref": ref, "text": text})
            if relative.endswith(".py"):
                for hit in symbols_for(text, repo=slug, path=relative, ref=ref):
                    payload["symbols"].append(
                        {
                            "repo": hit.location.repo,
                            "path": hit.location.path,
                            "ref": hit.location.ref,
                            "name": hit.name,
                            "kind": hit.kind,
                            "start": hit.span.start,
                            "end": hit.span.end,
                        }
                    )
        print(f"  {slug}: {root}")
    path.write_text(json.dumps(payload), encoding="utf-8")
    print(
        f"  skipped: {skipped['large']} over {MAX_FILE_BYTES} bytes, {skipped['binary']} not UTF-8"
    )
    return payload, payload["symbols"]


def _recall(
    client: httpx.Client, *, symbols: list[dict[str, Any]], sample: int, seed: int = 7
) -> dict[str, Any]:
    """How often the index hands back the file a symbol is defined in.

    Two questions, because they are two mechanisms: ``/symbols`` is the symbol table and
    ``/search`` is full text. A name that appears in forty files is not a failure of either —
    so the measure is *the defining file appears in the first page*, and ``top1`` is reported
    beside it because a definition at rank 19 is a different product from one at rank 0.

    ⚠️ The sample is bucketed by **ambiguity**, because that is what the misses turn out to
    be. On this corpus the symbol table holds every definition (a probe found 0 of 150
    sampled rows missing), and the misses are names with more definitions than a page holds:
    ``__init__`` alone has 576. A single recall number over such a sample measures the
    sample's name mix, not the index — so both the overall and the unambiguous figure are
    printed, and the gap between them is the truncation cost.
    """
    counts: dict[str, int] = {}
    for row in symbols:
        counts[row["name"]] = counts.get(row["name"], 0) + 1
    page = 50  # the default max_hits of a symbol query
    rng = random.Random(seed)
    chosen = rng.sample(symbols, min(sample, len(symbols)))
    tallies = {
        "all": [0, 0, 0, 0, 0],  # sampled, symbols hit, symbols top1, search hit, search top1
        "unambiguous": [0, 0, 0, 0, 0],
    }
    for row in chosen:
        bucket = "all" if counts[row["name"]] > page else "unambiguous"
        for key in ("all", bucket):
            tallies[key][0] += 1
        wanted = {"repos": [row["repo"]], "ref": row["ref"]}
        table = client.post("/symbols", json={**wanted, "name": row["name"]}).json()
        rows = [hit for hit in table.get("symbols", ()) if hit.get("path") == row["path"]]
        text_answer = client.post("/search", json={**wanted, "pattern": row["name"]}).json()
        hits = text_answer.get("hits", ())
        for key in ("all", bucket):
            if rows:
                tallies[key][1] += 1
                tallies[key][2] += 1 if table["symbols"][0].get("path") == row["path"] else 0
            if any(hit.get("path") == row["path"] for hit in hits):
                tallies[key][3] += 1
                tallies[key][4] += 1 if hits and hits[0].get("path") == row["path"] else 0

    def _row(values: list[int]) -> dict[str, Any]:
        total = values[0]
        return {
            "sampled": total,
            "symbols_recall": round(values[1] / total, 3) if total else 0.0,
            "symbols_top1": round(values[2] / total, 3) if total else 0.0,
            "search_recall": round(values[3] / total, 3) if total else 0.0,
            "search_top1": round(values[4] / total, 3) if total else 0.0,
        }

    return {"overall": _row(tallies["all"]), "unambiguous": _row(tallies["unambiguous"])}


def _latency(
    client: httpx.Client, *, slugs: list[str], names: list[str], queries: int
) -> dict[str, Any]:
    """Three shapes: an exact symbol name, a common word, and a narrowed glob."""
    shapes: dict[str, dict[str, Any]] = {}
    for label, payload in (
        ("symbol name", {"pattern": "clamp"}),
        ("common word", {"pattern": "def"}),
        ("narrow glob", {"pattern": "def", "globs": ["src/**"]}),
    ):
        latencies: list[float] = []
        hits = 0
        for index in range(queries):
            body = {**payload, "repos": slugs, "ref": None}
            if label == "symbol name":
                body["pattern"] = names[index % len(names)]
            started = time.perf_counter()
            answer = client.post("/search", json=body).json()
            latencies.append((time.perf_counter() - started) * 1000.0)
            hits += len(answer.get("hits", ()))
        shapes[label] = {
            "hits": hits,
            "p50_ms": round(_percentile(latencies, 0.50), 2),
            "p95_ms": round(_percentile(latencies, 0.95), 2),
            "max_ms": round(max(latencies), 2),
        }
    return shapes


def run_real(args: argparse.Namespace) -> int:
    from forgepilot.seams.refs import RepoRef

    targets: list[tuple[str, Path]] = []
    for entry in args.corpus:
        slug, _, where = entry.partition("=")
        parts = slug.split("/")
        if len(parts) != 3 or not all(parts):
            print(f"not a repository slug: {slug!r} (want host/namespace/name)", file=sys.stderr)
            return 2
        repo = RepoRef(host=parts[0], namespace=parts[1], name=parts[2])
        root = Path(where).resolve()
        if not (root / ".git").exists():
            print(f"not a checkout: {root}", file=sys.stderr)
            return 2
        targets.append((repo.slug, root))
    if not targets:
        print("--corpus needs at least one SLUG=PATH", file=sys.stderr)
        return 2

    work = args.work or (FORGE / ".tmp" / "bench-real")
    work.mkdir(parents=True, exist_ok=True)
    index = work / "index.json"
    started = time.perf_counter()
    payload, symbols = build_real_corpus(targets, index)
    build_s = time.perf_counter() - started
    text_bytes = sum(len(row["text"]) for row in payload["files"])
    print(
        f"corpus: {len(payload['files'])} file(s), {len(symbols)} symbol(s), "
        f"{text_bytes / 1e6:.1f} MB of text, {index.stat().st_size / 1e6:.1f} MB on disk, "
        f"built in {build_s:.1f}s"
    )

    slugs = [slug for slug, _ in targets]
    process, base = _start(index, layout=args.shard_by)
    try:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        with httpx.Client(base_url=base, headers=headers, timeout=120.0) as client:
            client.post("/search", json={"pattern": "def", "repos": slugs, "ref": None})
            latency = _latency(
                client, slugs=slugs, names=[row["name"] for row in symbols], queries=args.queries
            )
            recall = _recall(client, symbols=symbols, sample=args.sample)
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged process
            process.kill()
            process.wait(timeout=20)

    print()
    print("| query shape | hits | p50 ms | p95 ms | max ms |")
    print("|---|---|---|---|---|")
    for label, row in latency.items():
        print(f"| {label} | {row['hits']} | {row['p50_ms']} | {row['p95_ms']} | {row['max_ms']} |")
    print()
    print(
        "| recall | n | `/symbols` definition | `/symbols` rank 1 | `/search` definition | `/search` rank 1 |"
    )
    print("|---|---|---|---|---|---|")
    for label, row in recall.items():
        print(
            f"| {label} | {row['sampled']} | {row['symbols_recall']} | {row['symbols_top1']} | "
            f"{row['search_recall']} | {row['search_top1']} |"
        )
    print()
    print(json.dumps({"files": len(payload["files"]), "symbols": len(symbols), **recall}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="measure the sharded index")
    parser.add_argument("--files", type=int, default=10_000)
    parser.add_argument("--repos", type=int, default=20)
    parser.add_argument("--queries", type=int, default=60)
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument(
        "--corpus",
        action="append",
        default=[],
        metavar="SLUG=PATH",
        help="index a real checkout instead of the synthetic corpus (repeatable)",
    )
    parser.add_argument("--sample", type=int, default=200, help="symbols to sample for recall")
    parser.add_argument("--shard-by", choices=("repo", "top-dir"), default="repo")
    args = parser.parse_args(argv)

    if args.corpus:
        return run_real(args)

    work = args.work or (FORGE / ".tmp" / "bench-index")
    work.mkdir(parents=True, exist_ok=True)
    index = work / "index.json"
    built = time.perf_counter()
    payload = build_corpus(index, files=args.files, repos=args.repos)
    build_s = time.perf_counter() - built
    print(
        f"corpus: {len(payload['files'])} file(s), {len(payload['symbols'])} symbol(s), "
        f"{index.stat().st_size / 1e6:.1f} MB, built in {build_s:.1f}s"
    )

    results: dict[str, Any] = {}
    for layout in ("repo", "top-dir"):
        process, base = _start(index, layout=layout)
        try:
            results[layout] = measure(base, repos=args.repos, queries=args.queries)
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged process
                process.kill()
                process.wait(timeout=20)
        print(layout, json.dumps(results[layout]))

    print()
    print("| layout | shards | query | hits | p50 ms | p95 ms | max ms |")
    print("|---|---|---|---|---|---|---|")
    for layout, row in results.items():
        for shape, label in (("all", "every shard"), ("narrow", "globs mod03/**")):
            measured = row[shape]
            print(
                f"| {layout} | {row['shards']} | {label} | {measured['hits']} | "
                f"{measured['p50_ms']} | {measured['p95_ms']} | {measured['max_ms']} |"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
