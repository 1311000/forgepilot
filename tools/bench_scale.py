"""A second scale data point: 60 000 files, on one workstation, measured instead of guessed.

P21 measured 10 020 synthetic files and P26 measured 2 316 real ones. Both are small enough
that "p95 is fine at 2 000 files" says nothing about a corpus an enterprise would call small.
This generates a tree large enough to be uncomfortable (60 000 files by default), builds the
index, and reports where the time goes: build seconds, index size, search p95 per query shape,
and the **ingest rate** a delta achieves over the real socket.

Two honest limits, stated here so a reader does not have to hunt for them. The corpus is
synthetic — same shapes as a service tree, none of the entropy — so recall numbers would be
meaningless here and are deliberately not printed; P26 is where recall is measured, on real
code. And the service is the reference one from `tools/serve_index.py`, which scans Python
objects: these are *not* enterprise p95 numbers, they are the number below which a real index
has to be, to be worth deploying.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from forgepilot.providers.retrieve.index import (  # noqa: E402
    IndexCorpus,
    IndexedFile,
    symbols_for,
)
from forgepilot.seams.refs import RepoRef  # noqa: E402
from forgepilot.seams.retriever import SearchQuery  # noqa: E402
from forgepilot.seams.security import ExecutionConstraints  # noqa: E402


#: Nearest-rank percentiles, the same rule `tools/load_single_replica.py` uses. Loaded from
#: that file rather than restated: two percentile definitions in one repository is how a
#: report starts disagreeing with the tool it quotes.
def _percentile():
    path = ROOT / "tools" / "load_single_replica.py"
    spec = importlib.util.spec_from_file_location("load_single_replica_percentile", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.percentile


percentile = _percentile()

FUNCTIONS_PER_FILE = 4
NEEDLE_EVERY = 100


def generate(root: Path, *, files: int, repos: int, seed: int = 7) -> list[tuple[str, str]]:
    """Write `files` Python files across `repos` synthetic repositories.

    Returns `(repo_slug, relative_path)` pairs. Content is generated from the file index, not
    from a stream of random bytes, so two runs produce byte-identical trees and a number can be
    re-measured. `seed` perturbs only line order inside a function body.
    """

    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, str]] = []
    per_repo = max(files // max(repos, 1), 1)
    for repo_index in range(repos):
        slug = f"git.local/scale/r{repo_index:02d}"
        for local in range(per_repo):
            package = f"pkg{local % 10:02d}"
            path = f"{package}/svc_{local:05d}.py"
            body = [f'"""Module {repo_index}/{local}, generated for the scale benchmark."""', ""]
            body.append(f"from {package} import helper_{local % 7}")
            body.append("")
            for fn in range(FUNCTIONS_PER_FILE):
                body.append(f"def compute_{local:05d}_{fn}(value, limit):")
                body.append(f'    """Clamp {fn}; the body is noise, the symbol is the point."""')
                body.append(f"    scale = {rng.randint(1, 9)}")
                body.append(f"    return min(value * scale, limit) + {fn}")
                body.append("")
            if local % NEEDLE_EVERY == 0:
                body.append(f'NEEDLE = "needle_{repo_index:02d}_{local:05d}"')
                body.append("")
            target = root / slug.replace("/", "_") / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(body), encoding="utf-8")
            written.append((slug, path))
    return written


def corpus_of(
    root: Path, written: list[tuple[str, str]], *, ref: str
) -> tuple[list[IndexedFile], list]:
    """Read the files back and parse them, the way the builder does — but in memory."""

    files: list[IndexedFile] = []
    symbols = []
    for slug, path in written:
        text = (root / slug.replace("/", "_") / path).read_text(encoding="utf-8")
        host, namespace, name = slug.split("/")
        files.append(
            IndexedFile(repo=RepoRef(host, namespace, name), path=path, ref=ref, text=text)
        )
        symbols.extend(symbols_for(text, repo=slug, path=path, ref=ref))
    return files, symbols


def constraints_for(slugs: set[str]) -> ExecutionConstraints:
    repos = []
    for slug in sorted(slugs):
        host, namespace, name = slug.split("/")
        repos.append(RepoRef(host=host, namespace=namespace, name=name))
    return ExecutionConstraints(
        principal_id="scale-bench",
        tenant_id="default",
        allowed_repos=frozenset(repos),
    )


async def measure_queries(
    corpus: IndexCorpus, constraints: ExecutionConstraints, *, repeats: int, ref: str
) -> dict[str, list[float]]:
    """Three shapes, because they cost different amounts: rare token, common token, glob."""

    repos = tuple(constraints.allowed_repos)
    shapes = {
        "needle": "needle_",
        "def": "def",
        "glob": "def",
    }
    measured: dict[str, list[float]] = {}
    for label, pattern in shapes.items():
        globs = ("pkg03/**",) if label == "glob" else ()
        samples: list[float] = []
        for _ in range(repeats):
            query = SearchQuery(pattern=pattern, repos=repos, ref=ref, globs=globs, max_hits=200)
            started = time.perf_counter()
            await corpus.search(query, constraints)
            samples.append((time.perf_counter() - started) * 1000.0)
        measured[label] = samples
    return measured


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(base: str, process: subprocess.Popen, timeout_s: float = 30.0) -> None:
    import httpx

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the index service exited with {process.returncode}")
        try:
            httpx.get(f"{base}/status", timeout=1.0, trust_env=False)
            return
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.1)
    raise RuntimeError("the index service did not come up")


def http_latency_and_ingest(
    index_path: Path,
    *,
    repeats: int,
    delta_files: int,
    log_path: Path,
) -> tuple[dict[str, list[float]], float, int]:
    """Search p95 through the real socket, and how long a delta takes to ingest."""

    import httpx

    from forgepilot.providers.retrieve.remote import HttpIndexWriter
    from forgepilot.seams.index_service import IndexUpdate

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools" / "serve_index.py"),
            "--index",
            str(index_path),
            "--port",
            str(port),
            "--token",
            "bench=*",
            "--write-token",
            "bench",
        ],
        stdout=subprocess.DEVNULL,
        stderr=handle,
        cwd=str(ROOT),
    )
    try:
        _wait_for(base, process)
        client = httpx.Client(
            base_url=base, headers={"Authorization": "Bearer bench"}, trust_env=False
        )
        shapes = {"needle": "needle_", "def": "def", "glob": "pkg03/**"}
        measured: dict[str, list[float]] = {}
        for label, pattern in shapes.items():
            payload = {
                "pattern": pattern,
                "repos": [f"git.local/scale/r{index:02d}" for index in range(20)],
                "ref": "<worktree>",
                "globs": [pattern] if label == "glob" else [],
                "maxHits": 200,
            }
            samples: list[float] = []
            for _ in range(repeats):
                started = time.perf_counter()
                response = client.post("/search", json=payload)
                response.raise_for_status()
                samples.append((time.perf_counter() - started) * 1000.0)
            measured[label] = samples

        writer = HttpIndexWriter(base, token="bench", timeout_s=60.0)
        constraints = constraints_for({f"git.local/scale/r{index:02d}" for index in range(20)})
        documents = [
            _document(f"pkg03/ingest_{index:04d}.py", index) for index in range(delta_files)
        ]
        update = IndexUpdate(
            repo=RepoRef("git.local", "scale", "r00"),
            ref="<worktree>",
            documents=tuple(documents),
        )
        started = time.perf_counter()
        asyncio.run(writer.apply(update, constraints))
        elapsed = time.perf_counter() - started
        client.close()
        return measured, elapsed, delta_files
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server
            process.kill()
        handle.close()


def _document(path: str, index: int):
    from forgepilot.seams.index_service import IndexDocument

    return IndexDocument(
        path=path,
        text=f'"""Ingested file {index}."""\n\n\ndef ingested_{index}(value):\n    return value\n',
    )


def summarise(samples: list[float]) -> dict[str, float]:
    return {
        "n": float(len(samples)),
        "p50": round(percentile(samples, 0.50), 1),
        "p95": round(percentile(samples, 0.95), 1),
        "p99": round(percentile(samples, 0.99), 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=60000)
    parser.add_argument("--repos", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=200)
    parser.add_argument("--delta", type=int, default=200, help="files in the ingest delta")
    parser.add_argument("--skip-http", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=Path, default=None, help="keep the generated tree here")
    args = parser.parse_args(argv)

    root = args.out or Path(tempfile.mkdtemp(prefix="forgepilot-scale-"))
    started = time.perf_counter()
    written = generate(root, files=args.files, repos=args.repos)
    generated_s = time.perf_counter() - started

    started = time.perf_counter()
    files, symbols = corpus_of(root, written, ref="<worktree>")
    build_s = time.perf_counter() - started

    corpus = IndexCorpus(files, symbols, max_hits=200)
    constraints = constraints_for({slug for slug, _ in written})

    measured = asyncio.run(
        measure_queries(corpus, constraints, repeats=args.repeats, ref="<worktree>")
    )

    index_path = root / "scale.index.json"
    payload = {
        "files": [
            {"repo": item.repo.slug, "path": item.path, "ref": item.ref, "text": item.text}
            for item in files
        ],
        "symbols": [
            {
                "repo": hit.location.repo,
                "path": hit.location.path,
                "ref": hit.location.ref,
                "name": hit.name,
                "kind": str(hit.kind),
                "start": hit.span.start,
                "end": hit.span.end,
                "signature": hit.signature,
            }
            for hit in symbols
        ],
    }
    index_path.write_text(json.dumps(payload), encoding="utf-8")

    report: dict[str, object] = {
        "files": len(files),
        "symbols": len(symbols),
        "textMb": round(sum(len(item.text) for item in files) / 1e6, 1),
        "indexMb": round(index_path.stat().st_size / 1e6, 1),
        "generateSeconds": round(generated_s, 1),
        "buildSeconds": round(build_s, 1),
        "searchMs": {label: summarise(samples) for label, samples in measured.items()},
    }

    if not args.skip_http:
        http_measured, ingest_s, ingested = http_latency_and_ingest(
            index_path,
            repeats=max(args.repeats // 4, 10),
            delta_files=args.delta,
            log_path=root / "serve.log",
        )
        report["httpSearchMs"] = {
            label: summarise(samples) for label, samples in http_measured.items()
        }
        report["ingest"] = {
            "files": ingested,
            "seconds": round(ingest_s, 2),
            "filesPerSecond": round(ingested / ingest_s, 1) if ingest_s else 0.0,
        }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"files {report['files']}, symbols {report['symbols']}, "
            f"text {report['textMb']} MB, index {report['indexMb']} MB"
        )
        print(f"generate {report['generateSeconds']} s, build {report['buildSeconds']} s")
        for label, row in report["searchMs"].items():  # type: ignore[union-attr]
            print(f"  in-process {label:6s} p50 {row['p50']:7.1f} ms  p95 {row['p95']:7.1f} ms")
        for label, row in report.get("httpSearchMs", {}).items():  # type: ignore[union-attr]
            print(f"  http       {label:6s} p50 {row['p50']:7.1f} ms  p95 {row['p95']:7.1f} ms")
        ingest = report.get("ingest")
        if isinstance(ingest, dict):
            print(
                f"  ingest {ingest['files']} files in {ingest['seconds']} s "
                f"({ingest['filesPerSecond']} files/s)"
            )
    if args.out is None:
        # 	empfile's last resort on a box with no writable %TEMP% is the current directory,
        # and a 60 000-file tree beside the source is a checkout nobody asked for.
        shutil.rmtree(root, ignore_errors=True)
    else:
        print(f"tree: {root}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
