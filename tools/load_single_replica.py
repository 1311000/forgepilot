"""Drive one replica of the API and print what it costs.

`docs/notes/deployment.md` has carried a single-replica SLO since M26 with nothing behind it
but a promise: `/readyz`, a drain, a `forgepilot_ready` gauge, and alert rules whose thresholds
were measured against *turn durations in session logs* — not against a server under load. This
tool is the load: it starts (or is pointed at) one replica, drives concurrent turns and reads,
and prints percentiles instead of adjectives.

Three things it is deliberately careful about, each learned the hard way in this repository:

* **One session per worker.** `POST /sessions/{id}/messages` answers **409** while a turn is
  running on that session (`SessionBusy`) — that is the design, not a failure. Load spread
  across N sessions measures the server; load on one session measures the busy flag.
* **`trust_env=False`.** A stale registry proxy on this box once made every loopback request
  fail with `WinError 10061`; an HTTP client that inherits the environment inherits that.
* **Percentiles by nearest rank.** With `n` samples the p95 is the sample at index
  `ceil(0.95·n)−1`, so a p95 can never be a value no request took, and a run of 20 requests
  reports a value that actually happened. Interpolating between two samples would print a
  latency nobody experienced, which is the kind of number this project refuses to ship.

It measures and prints; it does not decide. What it does *not* prove is written in the note.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONCURRENCY = 8
DEFAULT_REQUESTS = 40
DEFAULT_TIMEOUT_S = 30.0
TOKEN = "load-token"
#: The key the replica signs and verifies credentials with. Read from the environment (never
#: from a profile), and a fixed value here so a test never depends on the machine's `.env.local`.
SIGNING_KEY = "load-test-signing-key"


def percentile(samples: Sequence[float], q: float) -> float:
    """The nearest-rank percentile: a value that some request actually took.

    ``q`` is in ``[0, 1]``. An empty sample returns ``0.0`` — a caller that has no samples
    has no latency, and inventing one would put a number in a table that no request produced.
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be between 0 and 1, not {q!r}")
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, math.ceil(q * len(ordered)) - 1)
    return ordered[min(index, len(ordered) - 1)]


@dataclass
class Tally:
    """Samples and non-2xx answers for one kind of request."""

    samples: list[float] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)

    def record(self, seconds: float, status: int) -> None:
        self.samples.append(seconds)
        if status >= 300:
            self.failures[str(status)] = self.failures.get(str(status), 0) + 1

    @property
    def ok(self) -> int:
        return len(self.samples) - sum(self.failures.values())

    def row(self, kind: str) -> str:
        return (
            f"| {kind} | {len(self.samples)} | {self.ok} | "
            f"{', '.join(f'{k}: {v}' for k, v in sorted(self.failures.items())) or '-'} | "
            f"{percentile(self.samples, 0.5) * 1000:.0f} | "
            f"{percentile(self.samples, 0.95) * 1000:.0f} | "
            f"{percentile(self.samples, 0.99) * 1000:.0f} | "
            f"{max(self.samples, default=0.0) * 1000:.0f} |"
        )


@dataclass
class Report:
    tallies: dict[str, Tally]
    wall_s: float

    @property
    def requests(self) -> int:
        return sum(len(tally.samples) for tally in self.tallies.values())

    @property
    def failures(self) -> int:
        return sum(sum(tally.failures.values()) for tally in self.tallies.values())

    def render(self, *, concurrency: int) -> str:
        lines = [
            f"concurrency: {concurrency}   wall: {self.wall_s:.2f} s   "
            f"requests: {self.requests}   failures: {self.failures}   "
            f"rate: {self.requests / self.wall_s if self.wall_s else 0:.1f} req/s",
            "",
            "| request | n | ok | failures | p50 ms | p95 ms | p99 ms | max ms |",
            "|---|---|---|---|---|---|---|---|",
        ]
        lines.extend(tally.row(kind) for kind, tally in self.tallies.items() if tally.samples)
        return "\n".join(lines)


class Driver:
    """One HTTP client, one session per worker, three kinds of request."""

    def __init__(self, base_url: str, *, token: str = TOKEN, timeout_s: float = DEFAULT_TIMEOUT_S):
        import httpx

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_s,
            # Loopback and a stale registry proxy: see the module docstring.
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _timed(self, kind: str, tally: Tally, method: str, url: str, **kwargs: Any) -> int:
        started = time.perf_counter()
        response = await self._client.request(method, url, **kwargs)
        tally.record(time.perf_counter() - started, response.status_code)
        return response.status_code

    async def session(self) -> str:
        response = await self._client.post("/sessions")
        response.raise_for_status()
        return str(response.json()["sessionId"])

    async def turn(self, session_id: str, tally: Tally, text: str) -> None:
        """Post a turn and wait for it to finish.

        The `202` means *accepted and running in the background*, not *done*: posting again
        immediately is refused with **409** because a session runs one turn at a time — that
        is the design (`SessionBusy`), and a load driver that reads the 409 as a server
        failure measures its own mistake. So the time recorded here is end-to-end (queued →
        finished), which is what a user waits for, not the time to accept a request.
        """
        started = time.perf_counter()
        response = await self._client.post(f"/sessions/{session_id}/messages", json={"text": text})
        if response.status_code >= 300:
            tally.record(time.perf_counter() - started, response.status_code)
            return
        await self._wait_for_turn(session_id)
        tally.record(time.perf_counter() - started, 200)

    async def _wait_for_turn(self, session_id: str, *, timeout_s: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            response = await self._client.get(f"/sessions/{session_id}")
            if response.status_code < 300 and not response.json().get("running", False):
                return
            await asyncio.sleep(0.005)
        raise TimeoutError(f"the turn on {session_id} did not finish within {timeout_s:.0f}s")

    async def read(self, session_id: str, tallies: dict[str, Tally]) -> None:
        await self._timed("read", tallies["read"], "GET", f"/sessions/{session_id}")
        await self._timed("metrics", tallies["metrics"], "GET", "/metrics")

    async def stream(self, session_id: str, tally: Tally, *, want: int = 3) -> None:
        """Time to the first event, and whether `want` events arrived before the deadline."""
        started = time.perf_counter()
        seen = 0
        async with self._client.stream("GET", f"/sessions/{session_id}/events") as response:
            if response.status_code >= 300:
                await response.aread()
                tally.record(time.perf_counter() - started, response.status_code)
                return
            async for line in response.aiter_lines():
                if line.startswith("event:") or line.startswith("data:"):
                    seen += 1
                if seen >= want:
                    break
        tally.record(time.perf_counter() - started, 200 if seen else 599)


async def drive(
    base_url: str,
    *,
    concurrency: int,
    requests_per_worker: int,
    kinds: Iterable[str],
) -> Report:
    """Run the load and fold every request into one report."""
    tallies: dict[str, Tally] = defaultdict(Tally)
    for kind in ("session", "turn", "read", "metrics", "stream"):
        tallies[kind]  # keep a stable row order even when a kind is not exercised
    driver = Driver(base_url)
    kinds = tuple(kinds)
    started = time.perf_counter()
    try:

        async def worker(_: int) -> None:
            session_id = await driver.session()
            tallies["session"].record(0.0, 200)
            for step in range(requests_per_worker):
                if "turns" in kinds:
                    await driver.turn(session_id, tallies["turn"], f"load step {step}")
                if "reads" in kinds:
                    await driver.read(session_id, tallies)
                if "streams" in kinds and step == 0:
                    await driver.stream(session_id, tallies["stream"])

        await asyncio.gather(*(worker(index) for index in range(concurrency)))
    finally:
        await driver.aclose()
    return Report(dict(tallies), time.perf_counter() - started)


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def start_server(
    *, state: Path, workspaces: Path, port: int, echo_model: bool = True
) -> subprocess.Popen[bytes]:
    """Start one replica with the default profile and the writable directories pinned.

    ``echo_model`` (the default) overrides the `llm` row with the zero-latency provider: a load
    test that includes a vendor's 1.4 s latency measures the vendor, and one that spends money
    per run is a test nobody runs. ``--real-model`` is for the opposite question.
    """
    environment = dict(os.environ)
    environment["FORGEPILOT_HOME"] = str(state)
    environment["FORGEPILOT_TMP"] = str(workspaces)
    environment["FORGEPILOT_DEV_TOKENS"] = json.dumps(
        [{"token": TOKEN, "principal_id": "alice", "tenant_id": "default"}]
    )
    # The deployment's signing key, and it is not optional: without one the credentials it
    # issues are unsigned and `authenticate` — correctly — refuses to open a scope from a
    # credential nobody can verify. On a developer machine `.env.local` usually supplies this,
    # which is exactly how CI failed with a 401 while the same test passed locally.
    environment["FORGEPILOT_SIGNING_KEY"] = SIGNING_KEY
    command = [
        sys.executable,
        "-m",
        "forgepilot",
        "--config-dir",
        "configs",
        "--serve",
        "--port",
        str(port),
    ]
    if echo_model:
        command.extend(["--patch", "src/forgepilot/bundles/llm_echo/patch.yaml"])
    return subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
    )


def wait_until_healthy(
    base_url: str, *, process: subprocess.Popen[bytes], timeout_s: float
) -> None:
    import httpx

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"the server exited with {process.returncode} before answering")
        try:
            with httpx.Client(trust_env=False, timeout=1.0) as probe:
                if probe.get(f"{base_url}/healthz").status_code == 200:
                    return
        except Exception:  # noqa: BLE001 - not up yet is the normal case
            pass
        time.sleep(0.1)
    raise SystemExit(f"the server did not answer /healthz within {timeout_s:.0f}s")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=None, help="drive an already running replica")
    parser.add_argument("--start", action="store_true", help="start and stop one replica here")
    parser.add_argument("--port", type=int, default=0, help="with --start: the port (default: any)")
    parser.add_argument("--state", default=".tmp/load-state", help="with --start: FORGEPILOT_HOME")
    parser.add_argument(
        "--workspaces", default=".tmp/load-work", help="with --start: FORGEPILOT_TMP"
    )
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS, help="per worker")
    parser.add_argument(
        "--kinds",
        default="turns,reads",
        help="comma separated: turns, reads, streams",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable rows")
    parser.add_argument(
        "--real-model",
        action="store_true",
        help=(
            "with --start: keep the deployment's own `llm` row. The default overrides it with "
            "the zero-latency echo provider, so the numbers are this server's and the run is free"
        ),
    )
    args = parser.parse_args(argv)

    if not args.url and not args.start:
        print("error: pass --url, or --start to run a replica here", file=sys.stderr)
        return 2
    if args.concurrency < 1 or args.requests < 1:
        print("error: --concurrency and --requests must be at least 1", file=sys.stderr)
        return 2
    kinds = tuple(part.strip() for part in str(args.kinds).split(",") if part.strip())
    unknown = set(kinds) - {"turns", "reads", "streams"}
    if unknown:
        print(f"error: unknown --kinds value(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    process: subprocess.Popen[bytes] | None = None
    if args.start:
        port = args.port or free_port()
        state = Path(args.state).resolve()
        workspaces = Path(args.workspaces).resolve()
        state.mkdir(parents=True, exist_ok=True)
        workspaces.mkdir(parents=True, exist_ok=True)
        process = start_server(
            state=state, workspaces=workspaces, port=port, echo_model=not args.real_model
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_until_healthy(base_url, process=process, timeout_s=90.0)
        except SystemExit:
            process.terminate()
            raise
    else:
        base_url = str(args.url)

    try:
        report = asyncio.run(
            drive(
                base_url,
                concurrency=args.concurrency,
                requests_per_worker=args.requests,
                kinds=kinds,
            )
        )
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:  # pragma: no cover - only a wedged server
                process.kill()
                process.wait(timeout=20)

    if args.json:
        for kind, tally in report.tallies.items():
            print(
                json.dumps(
                    {
                        "kind": kind,
                        "n": len(tally.samples),
                        "ok": tally.ok,
                        "failures": tally.failures,
                        "p50_ms": round(percentile(tally.samples, 0.5) * 1000),
                        "p95_ms": round(percentile(tally.samples, 0.95) * 1000),
                        "p99_ms": round(percentile(tally.samples, 0.99) * 1000),
                        "max_ms": round(max(tally.samples, default=0.0) * 1000),
                    }
                )
            )
        print(json.dumps({"wall_s": round(report.wall_s, 3), "requests": report.requests}))
    else:
        print(report.render(concurrency=args.concurrency))
    # A run that produced no turn or read at all proves nothing; say so rather than print 0s.
    if report.requests <= args.concurrency:
        print("error: no request beyond session creation was made", file=sys.stderr)
        return 1
    return 0 if report.failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
