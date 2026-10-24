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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="measure the sharded index")
    parser.add_argument("--files", type=int, default=10_000)
    parser.add_argument("--repos", type=int, default=20)
    parser.add_argument("--queries", type=int, default=60)
    parser.add_argument("--work", type=Path, default=None)
    args = parser.parse_args(argv)

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
