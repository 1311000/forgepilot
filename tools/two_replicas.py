"""Two replicas, one state directory — does the lease cross a process boundary?

P16 built the seam (a lease, a shared mailbox, a log both processes can read) and proved it in
one interpreter with two `SessionRegistry` objects in it. That proof is weaker than it looks: two
registries sharing a dict are still one process, one event loop, and one set of live objects. The
promise a deployment rests on is that the *same* behaviour holds when the second replica is a
different process with its own memory — which is what this script exercises:

1. a session created on replica A, with a turn started there;
2. the same session posted to on replica B **while A is running it** — refused, and the refusal
   names the other replica rather than pretending the session is absent;
3. B reading the session A wrote, from the log A flushed (not from memory);
4. B streaming A's turn events from the shared log;
5. a turn that B runs itself, and A reading it back.

The model is `EchoLLMPlugin` with `delay_s` so the busy window is deterministic; nothing here
needs a vendor or a credential.

Usage:
    python tools/two_replicas.py            # start both replicas, run the scenario, stop them
    python tools/two_replicas.py --keep     # leave them running and print the two base URLs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Check", "environment", "free_port", "scenario", "start", "wait_healthy"]

#: The shared token table. Both replicas get the same one because they are one deployment; the
#: point of the test is the lease, not authentication.
TOKEN = "two-replica-token"

#: Not optional: without a key the credentials are unsigned and `authenticate` refuses to open a
#: scope from a credential nobody can verify (this is what made the load test fail in CI with a
#: 401 while passing on a developer machine whose `.env.local` supplied one).
SIGNING_KEY = "two-replica-signing-key"

#: How long a canned answer is held open. Long enough that the second replica's request lands
#: inside the window on any reasonable machine, short enough that the scenario takes a second.
DELAY_S = 0.6

DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True, slots=True)
class Check:
    """One assertion the scenario made, and what it saw."""

    name: str
    ok: bool
    detail: str


def free_port() -> int:
    """A port nobody is listening on right now (racy by nature; the bind below is the real check)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def environment(state: Path, workspaces: Path) -> dict[str, str]:
    """The environment both replicas share: one state directory, one token table, one key."""
    environment_ = dict(os.environ)
    environment_["FORGEPILOT_HOME"] = str(state)
    environment_["FORGEPILOT_TMP"] = str(workspaces)
    environment_["FORGEPILOT_DEV_TOKENS"] = json.dumps(
        [{"token": TOKEN, "principal_id": "alice", "tenant_id": "default"}]
    )
    environment_["FORGEPILOT_SIGNING_KEY"] = SIGNING_KEY
    return environment_


def write_patch(state: Path) -> Path:
    """A patch file overriding the `llm` row with the echo provider, held open for ``DELAY_S``.

    A patch file rather than an environment variable because the row's config is where the whole
    profile is assembled, and a bundle that read an environment variable for its delay would be a
    provider with a hidden knob. The file lives in the state directory, so two runs never share it.
    """
    path = state / "two-replicas.yaml"
    path.write_text(
        "\n".join(
            [
                "# A canned answer held open for a moment: the lease is only observable while a",
                "# turn is running, and a real model would make this test cost money and time.",
                "- id: llm",
                "  plugin: forgepilot.providers.llm.echo:EchoLLMPlugin",
                "  tier: trusted",
                "  config:",
                "    model: echo-0",
                f"    delay_s: {DELAY_S}",
                "",
                "# The coordinator, without which `--multi-replica` is two servers that",
                "# each believe they own every session: no lease, no shared mailbox, and a",
                "# second replica answering 404 for a session the first one is running.",
                "- id: coordination",
                "  plugin: forgepilot.providers.coordination.files:FileCoordinationPlugin",
                "  tier: trusted",
                "  config:",
                "    state_dir: " + str(state),
                "    poll_s: 0.1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def start(
    *, root: Path, state: Path, workspaces: Path, port: int, patch: Path
) -> subprocess.Popen[bytes]:
    """Start one replica with ``--multi-replica``: it must not take the single-server lock."""
    command = [
        sys.executable,
        "-m",
        "forgepilot",
        "--config-dir",
        "configs",
        "--patch",
        str(patch),
        "--serve",
        "--multi-replica",
        "--port",
        str(port),
        "--workspace",
        str(root),
    ]
    # Each replica's output goes to its own file rather than a pipe: a pipe nobody reads fills
    # after 64 KB and then the server blocks while logging the very error you are looking for
    # (that is how P11's index service turned a 500 into a client ReadTimeout). The file also
    # outlives the process, which is what a failing CI run needs.
    log = (state / f"replica-{port}.log").open("w", encoding="utf-8")
    return subprocess.Popen(
        command,
        cwd=str(root),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=environment(state, workspaces),
    )


def wait_healthy(base_url: str, *, process: subprocess.Popen[bytes], timeout_s: float) -> None:
    """Poll ``/healthz`` until the replica answers, and fail loudly if the process died."""
    import httpx

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(
                f"a replica exited with {process.returncode} before answering; see the log in "
                f"the state directory"
            )
        try:
            with httpx.Client(trust_env=False, timeout=1.0) as probe:
                if probe.get(f"{base_url}/healthz").status_code == 200:
                    return
        except Exception:  # noqa: BLE001 - not up yet is the normal case
            pass
        time.sleep(0.1)
    raise SystemExit(f"a replica did not answer /healthz within {timeout_s:.0f}s")


async def _create(client: Any, base_url: str, headers: dict[str, str]) -> str:
    response = await client.post(f"{base_url}/sessions", json={}, headers=headers)
    response.raise_for_status()
    return str(response.json()["sessionId"])


async def _post(
    client: Any, base_url: str, session: str, text: str, headers: dict[str, str]
) -> Any:
    return await client.post(
        f"{base_url}/sessions/{session}/messages", json={"text": text}, headers=headers
    )


async def _wait_idle(
    client: Any, base_url: str, session: str, headers: dict[str, str], *, timeout_s: float
) -> dict[str, Any]:
    """Poll until the session says it is not running, then return the detail body."""
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"{base_url}/sessions/{session}", headers=headers)
        response.raise_for_status()
        last = response.json()
        if last.get("running") is False:
            return last
        await asyncio.sleep(0.02)
    raise TimeoutError(f"the session never stopped running; last seen {last!r}")


async def _wait_for_usage(
    client: Any, base_url: str, session: str, headers: dict[str, str], *, timeout_s: float
) -> tuple[dict[str, Any], float]:
    """Poll until the log shows a priced model call, and report how long that took.

    A replica flushes the log when a turn ends, so another replica can be a few
    milliseconds behind. That lag is a fact about the design (the log is the shared
    truth, not memory), not a defect -- so the scenario measures it instead of
    sleeping an arbitrary amount and hoping.
    """
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"{base_url}/sessions/{session}", headers=headers)
        response.raise_for_status()
        last = response.json()
        if (last.get("usage") or {}).get("calls", 0) >= 1:
            break
        await asyncio.sleep(0.02)
    return last, (time.monotonic() - started) * 1000.0


async def _stream_until(
    client: Any,
    base_url: str,
    session: str,
    needle: str,
    headers: dict[str, str],
    *,
    timeout_s: float,
) -> str:
    """Read the event stream until ``needle`` appears in a frame, and return what was read."""
    import httpx

    seen: list[str] = []
    deadline = time.monotonic() + timeout_s
    # A shorter *read* timeout than the overall budget: the stream is infinite by design (it
    # keeps polling the store for new events), so "nothing arrived for a second" is the normal
    # way to learn that the log has been fully replayed -- not a failure.
    try:
        async with client.stream(
            "GET",
            f"{base_url}/sessions/{session}/events",
            headers=headers,
            timeout=httpx.Timeout(timeout_s, read=1.0),
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                seen.append(line)
                if needle in line:
                    return "\n".join(seen)
                if time.monotonic() > deadline:
                    break
    except httpx.TimeoutException:
        pass
    raise TimeoutError(f"the stream never carried {needle!r}; read:\n" + "\n".join(seen[:40]))


async def scenario(
    replica_a: str, replica_b: str, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> list[Check]:
    """Run the cross-process checks and return what each one saw. No assertion here: the caller
    decides what a failure means, so the script can print a table instead of a traceback."""
    import httpx

    checks: list[Check] = []
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(trust_env=False, timeout=timeout_s) as client:
        session = await _create(client, replica_a, headers)
        checks.append(Check("a session is created on replica A", bool(session), session))

        started = await _post(client, replica_a, session, "hello from A", headers)
        checks.append(
            Check(
                "A starts a turn",
                started.status_code == 202,
                f"{started.status_code} {started.text[:120]}",
            )
        )

        refused = await _post(client, replica_b, session, "hello from B", headers)
        text = refused.text.lower()
        checks.append(
            Check(
                "B is refused while A runs it, and the refusal names the other replica",
                refused.status_code in (409, 503) and ("replica" in text or "elsewhere" in text),
                f"{refused.status_code} {refused.text[:160]}",
            )
        )

        await _wait_idle(client, replica_a, session, headers, timeout_s=timeout_s)
        read_back, lag_ms = await _wait_for_usage(
            client, replica_b, session, headers, timeout_s=timeout_s
        )
        checks.append(
            Check(
                "B reads the log A wrote",
                read_back.get("running") is False
                and (read_back.get("usage") or {}).get("calls", 0) >= 1,
                f"after {lag_ms:.0f} ms: "
                + json.dumps(
                    {"running": read_back.get("running"), "usage": read_back.get("usage")},
                    ensure_ascii=False,
                ),
            )
        )

        streamed = await _stream_until(
            client, replica_b, session, "turn/end", headers, timeout_s=timeout_s
        )
        checks.append(
            Check("B streams A's turn from the shared log", "turn/end" in streamed, streamed[-200:])
        )

        other = await _create(client, replica_b, headers)
        started_b = await _post(client, replica_b, other, "hello from B", headers)
        waited = await _wait_idle(client, replica_b, other, headers, timeout_s=timeout_s)
        checks.append(
            Check(
                "B runs its own turn",
                started_b.status_code == 202 and waited.get("running") is False,
                f"{started_b.status_code} running={waited.get('running')}",
            )
        )

        back_on_a, lag_a_ms = await _wait_for_usage(
            client, replica_a, other, headers, timeout_s=timeout_s
        )
        checks.append(
            Check(
                "A reads the session B ran",
                (back_on_a.get("usage") or {}).get("calls", 0) >= 1,
                f"after {lag_a_ms:.0f} ms: usage={back_on_a.get('usage')}",
            )
        )
    return checks


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - only when a drain hangs
        process.kill()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="the repository (default: this file's parent)")
    parser.add_argument(
        "--state", default=None, help="shared FORGEPILOT_HOME (default: a temp dir)"
    )
    parser.add_argument("--keep", action="store_true", help="leave both replicas running")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    # Not `tempfile.mkdtemp` without a parent argument: on this box the temp directory's last
    # resort is the current directory, which would leave a state tree in the repository root.
    state = Path(args.state).resolve() if args.state else root / ".tmp" / "two-replica-state"
    workspaces = state / "work"
    if state.exists() and not args.keep:
        import shutil

        shutil.rmtree(state, ignore_errors=True)
    state.mkdir(parents=True, exist_ok=True)
    workspaces.mkdir(parents=True, exist_ok=True)
    patch = write_patch(state)

    processes: list[subprocess.Popen[bytes]] = []
    bases: list[str] = []
    try:
        for _ in range(2):
            port = free_port()
            bases.append(f"http://127.0.0.1:{port}")
        for base in bases:
            port = int(base.rsplit(":", 1)[1])
            process = start(root=root, state=state, workspaces=workspaces, port=port, patch=patch)
            processes.append(process)
        for base, process in zip(bases, processes, strict=True):
            wait_healthy(base, process=process, timeout_s=args.timeout)

        if args.keep:
            print("replica A:", bases[0])
            print("replica B:", bases[1])
            print("state:    ", state)
            return 0

        checks = asyncio.run(scenario(bases[0], bases[1], timeout_s=args.timeout))
    finally:
        if not args.keep:
            for process in processes:
                _stop(process)

    for check in checks:
        print(f"{'ok  ' if check.ok else 'FAIL'}  {check.name}")
        if not check.ok:
            print(f"      {check.detail}")
    failed = [check for check in checks if not check.ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        return 1
    if not args.keep:
        import shutil

        shutil.rmtree(state, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
