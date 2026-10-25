# Deployment: one image, one process, one TLS terminator

This note is about running ForgePilot as **a service** rather than as a local CLI
— the container, what it needs, and the two properties (shutdown and
single-replica) that a browser and an orchestrator can each notice.

The seams are unchanged. A deployment that needs a different ticket source, repo
host, retriever or identity provider replaces a *row* in a bundle; nothing here
is a code branch for production.

## What the image is

`Dockerfile` is two stages:

- **build** installs the package and its `web` extra into wheels;
- **runtime** installs *only* those wheels, so what reaches production is the set
  the build resolved rather than whatever pip finds on rebuild day.

Then it stops:

- it runs as uid **10001** (`forgepilot`), because the sandbox's boundary is the
  workspace and a root process has already escaped it;
- `FORGEPILOT_HOME=/data`, on a **volume**, because the serve lock and the
  distillation watermark are durable state and a container that loses them on
  restart can start twice;
- `ripgrep` is installed, because the default retriever shells out to the real
  `rg` and deliberately has no Python fallback — without it the image boots and
  every `code_search` fails at the first tool call;
- the `HEALTHCHECK` asks `GET /healthz`, not the port, so a container that bound
  a socket but failed to assemble the app is **unhealthy** rather than "starting"
  forever.

`configs/` is baked in. That is what makes `--dump-config` and `--boot` in a
container answer the same question they answer on a laptop: *which composition is
running*.

## The single-replica rule, enforced rather than documented

`httpapi/registry.py` holds sessions in process memory. Two replicas over one
workspace each hold half the sessions and can stream only their own — a split
brain that looks like a working deployment until someone reconnects.

So `runtime/serve_lock.py` takes an advisory lock at
`$FORGEPILOT_HOME/serve.lock` before serving, and a second process **fails to
start** with the path and the recorded pid:

```
error: another ForgePilot is already serving this workspace: the lock
/data/serve.lock is held by pid 41. One process owns the session registry; run
two replicas only once the registry is shared.
```

Two things about that lock are load-bearing:

- **correctness comes from the kernel, not from the file.** A pid file survives a
  `kill -9` and needs a human to decide whether the pid is real; an `flock`/
  `LockFile` is released when the holder exits, however it exits.
- **the pid lives outside the locked region.** On Windows a byte-range lock also
  denies *reads* of the bytes it covers, so a pid written at offset 0 is
  unreadable by exactly the process that wants to report it. It is written at the
  start of the file and the locked byte sits at offset 4096.

`deploy/compose.yaml` therefore has no `deploy.replicas` key on `api`. Scaling
past one process is a **design change** — a shared registry and a message bus —
not a flag, and the lock exists so that the change cannot be skipped by accident.

## Shutdown: streams end, new work is refused

uvicorn's order is: stop accepting, wait for in-flight connections, *then* run the
ASGI lifespan shutdown. An SSE stream has no reason to finish, so that middle step
is the graceful-shutdown timeout long at best (unbounded by default). A workbench
tab left open would turn `Ctrl-C` — or a rolling deploy's `SIGTERM` — into a hang.

The signal handler therefore raises the flag **before** uvicorn waits
(`cli/main.py::_end_streams_on_signals`), and the consequences are:

| What | Behaviour | Why |
|---|---|---|
| SSE stream | ends *cleanly*, no synthetic final event | the client reconnects with `Last-Event-ID`; inventing an event would put something in the UI the log never said |
| `POST …/messages`, `POST /tasks` | `503` | not `409`: nothing is busy about the session, and another replica could accept it |
| an approval waiting on a human | released as `UNAVAILABLE` | every consumer already reads that as "no", so the turn takes its ordinary refusal path and the log records a refusal rather than a mystery cancellation |
| `GET /healthz` | still `200` | the drain must finish *before* the load balancer removes the instance; erroring first cuts streams at the proxy instead of ending them |

`timeout_graceful_shutdown` (15 s by default, `httpapi.shutdown_timeout` in the
profile) is a backstop for a stuck *reply*, not the mechanism.

## Metrics

`GET /metrics` speaks the Prometheus text format (`text/plain; version=0.0.4`).

- **Counts, in route templates.** `/sessions/{session_id}` is one series;
  a scanner probing `/.env`, `/nope-1`, `/nope-2` lands in one `unmatched` series.
- **Gauges are read at scrape time** from the registry that owns the truth —
  `forgepilot_sessions_live`, `forgepilot_turns_running`,
  `forgepilot_approvals_pending`. A pushed gauge is the one that silently stops
  falling.
- **No identifiers.** No tenant, principal, session or ticket in a label, which is
  also why the endpoint is deliberately unauthenticated: every number is a count,
  and a scrape endpoint that needs a credential is one a monitoring system cannot
  be given safely. The moment a label names a tenant, that stops being true and
  the handler has to grow an authorisation check in the same change.
- **No latency histogram.** Buckets should be chosen from measured traffic, and
  per-turn token counts already live in the session log, which is this platform's
  record. A second copy in a metric is a second thing to keep in sync.

## What the deployment needs

| Variable | Required | What it is |
|---|---|---|
| `FORGEPILOT_DEV_TOKENS` | yes (until OIDC lands) | JSON array of principals — see `api-and-workbench.md` |
| `FORGEPILOT_SIGNING_KEY` | yes | HMAC key for `ExecutionConstraints`; rotating it invalidates in-flight constraints by design |
| `FORGEPILOT_HOME` | yes (image sets `/data`) | durable state: serve lock, watermarks |
| `FORGEPILOT_TMP` | recommended | where worktrees are created; must be writable and **outside** the served checkout |
| `FORGEPILOT_LLM_API_KEY` / `_BASE_URL` / `_MODEL` | for a real model | without them, the replay provider answers deterministically and the server still serves |
| `FORGEPILOT_GITHUB_TOKEN` | to publish | `repo` scope; without it a task ends at `READY` (draft) rather than `DELIVERED` |
| `SITE_ADDRESS`, `ACME_EMAIL` | for `deploy/` | the proxy's hostname; `localhost` uses Caddy's internal CA |

Prerequisites that are not variables:

- **A project directory with `project.yaml`** mounted at `/workspace`. Chat works
  without one; task mode answers `409` naming the file, which is the designed
  behaviour rather than a missing default. The compose default points at
  `examples/stat-fix` as a *demonstration*, and that example's `test:` lines name
  `FORGEPILOT_PYTHON` — it needs `prepare_repo.py` run first (see its README), so
  a first container run should mount a project whose checks can actually run
  rather than the example.
- **`kernel.apparmor_restrict_unprivileged_userns=0` on Ubuntu 24.04+**, or the
  sandbox refuses to build its boundary — see `gates-on-linux.md`. On the pinned
  `ubuntu-22.04` base no host change is needed.
- **ripgrep** for `code_search` (in the image).

## P19 — the thresholds are measured, and one of them could never fire (2026-10-21)

`deploy/alerts.yml` used to carry five `for:` values chosen because they looked reasonable.
Two of them were wrong in a way only a reader of the code would notice:

- **`ForgePilotApprovalWaiting` was on `for: 15m` over a five-minute timeout.** An unanswered
  approval is a refusal after `DEFAULT_APPROVAL_TIMEOUT_S = 300.0`, so `approvals_pending`
  returns to 0 before a fifteen-minute window can elapse. The rule was monitoring that could not
  fire. It is now `4m` — below the timeout it has to catch — and a guard asserts that
  relationship against the constant itself rather than against a comment.
- **`ForgePilotTurnStall` was on `for: 30m`.** The 1343 live turns in this box's durable logs
  have p50 0.2 s, p95 28.0 s and a maximum of 242.5 s, so thirty minutes meant the alert would
  arrive after a stuck turn had long been abandoned by whoever was watching. It is now `10m`,
  about 2.5× the longest turn ever recorded here.

The measurement, and the page each alert needs, live in `docs/notes/runbook.md`. Two guards keep
the files from drifting: every alert must have a runbook page and every page must name a real
alert (both directions), and every `for:` must appear in the runbook's threshold table. Both were
falsified by reintroducing the old value and deleting a page; each failed on the sentence it was
written to say.

## P16 — more than one replica (2026-10-22)

`--serve --multi-replica` runs beside other replicas against one state directory.
It is a **single-writer, many-reader** shape, and the writer is per session:

- **One writer per session, by lease.** The log's `seq` is assigned by `Session`
  before a line is durable (a buffered event must carry its number for
  `source_event_seq`), so two processes appending one session would mint the same
  number twice. `seams/coordination.py` is the lease: `acquire` returns `None`
  while another holder's lease is live, `fencing` increments across takeovers, and
  the lease is renewed while a turn runs. A replica that loses its lease cancels
  the turn rather than finishing it into a log it no longer owns.
- **Approvals travel by the shared mailbox**, not by a process-local future: a
  question asked on A is answered on B, and both see it stop pending. The answer
  is created with `O_EXCL`, so exactly one replica wins and a second answer is
  refused rather than overwriting the first.
- **A session this replica is not running is read from the log.** `GET
  /sessions/{id}` and the event stream both fall back to storage; the stream polls
  by `seq` (there is no in-process signal a second process could raise) and starts
  with a `stream/open` frame so a load balancer sees the connection before the
  first event.
- **`--multi-replica` does not take the serve lock.** That lock's promise is "one
  process owns this state directory"; a deployment running replicas is saying the
  log arbitrates instead. The reference `deploy/compose.yaml` is unchanged — it
  still runs one replica, because the file system the replicas share is the
  deployment's decision, and a compose file cannot make one.

What it does **not** buy: cross-replica cancellation (a task started on A is not
stoppable from B), per-replica *drill-down* metrics beyond what `/metrics` answers
for one process (the two "is there work" alert rules already `sum()` across
replicas; `up` and `forgepilot_ready` stay per target, because "this replica is
draining" is a fact about one process), or a shared memory store (the single-file
SQLite store is local-only, which `docs/notes/memory.md` records as a decision
with this exact re-opening condition).

## What is deliberately not here

Kubernetes manifests, a Postgres-backed session registry, RS256/JWKS (M26's OIDC
provider verifies HS256 and refuses the others by name), and the enterprise
retrieval index. Each is a seam that a deployment plugs into, not a gap in the
seams — the list is in `CLAUDE.md` under the tracks still unstarted. The static
token table is no longer the only identity provider, but it is still the default
one and still what a dev box uses.

## M26 — the single-replica SLO, and what it can promise

The deployment above is one process by design (`serve.lock`), so the SLO it can
make is not "stay up" but **"when it goes down, nothing it accepted is lost and
nothing it published is wrong"**. Four pieces, each with the reader it was built
for:

- **`/readyz` versus `/healthz`.** `healthz` answers "is this process alive"; a
  draining process is alive and must still get no new work, because a load
  balancer that keeps sending it traffic opens SSE streams on a process that is
  about to end. `readyz` returns `503 {"status": "draining"}` once
  `registry.stop()` has run, and both read that one flag, so they cannot disagree.
  `FORGEPILOT_READY=0`? No: readiness is derived, never configured.
- **`--backup` / `--restore`** (`runtime/backup.py`). The archive carries
  `manifest.json` with a SHA-256 and a size per file; restoring verifies both and
  parses every `.jsonl` before anything is moved into place, in a staging
  directory beside the target. A backup **refuses while a server holds the lock**,
  because a copy taken during an append can catch half a line and `recover_tail`
  would then repair the damage on the next boot — a backup whose failure mode is
  "restores fine and is subtly wrong" is worse than a refused one. Two defects the
  first smoke test found: `Path("x.tar.gz").suffix` is `".gz"` (so every archive
  path a person types was treated as a directory), and the manifest was being
  moved into the restored directory as if it were state.
- **`forgepilot_ready` gauge + `deploy/alerts.yml`.** The gauge is 1 until the
  same `registry.stop()` flag flips, so an alert reading metrics agrees with a
  probe. `tests/httpapi/test_metrics.py::test_the_alert_rules_name_series_that_exist`
  scrapes `/metrics` and fails if a rule names a series this build does not emit —
  a rule pointing at nothing never fires and looks like monitoring.
- **OIDC (`providers/identity/oidc.py`, `bundles/identity_oidc/`).** Same
  `principal` seam, so `deps.py` cannot tell which provider answered. **HS256
  only**, because this build has no crypto library and refuses RS256 *by name*
  rather than failing with "invalid token"; group membership is not authorisation
  (unmapped groups grant nothing, and the mapping is in `--dump-config`).

**What it still does not promise.** No multi-replica (a second process is refused,
not made safe); no automated backup schedule (the flag is what a cron job calls);
no RS256/JWKS; no alert on "the agent is getting worse" — that lives in the
session logs, and a metric invented for it would be a number without a reader.

### Backing up a multi-replica deployment (P16 ④)

`forgepilot --backup` takes the single-replica state-directory lock for the duration of
the copy. Under `--multi-replica` no replica holds that lock, so the lock is not what
protects a backup there. Two roles do, and they exclude each other:

- **A writer heartbeat.** A replica joins the writer set the first time it starts a
  turn (`SessionRegistry._ensure_writer`) and renews that lease while it runs. It is
  joined lazily rather than at boot, so a replica that receives no work never appears
  as a writer — a backup refused by an idle process would be a false refusal, and a
  false refusal is how a lock gets removed entirely.
- **A deployment lease** (`deployment:maintenance`, TTL 600 s). `--backup` takes it
  before copying and releases it in `finally`. If any writer heartbeat is live, the
  take fails and the command exits 2 naming the holders:

  ```
  error: /srv/forgepilot is being written by /srv/.../host:4242; a backup of a live
  log can miss or tear an append. Stop the writer, or run the backup when it is idle.
  ```

  While the deployment lease is held, a new turn is refused with **503** (not 409: no
  session is busy, and another replica would refuse identically) and the message says
  to retry after the backup.

The lease directory is **excluded from the archive**, by any path part rather than by
filename — a lease says *this process is running now*, so a restored one would either
refuse writers for its whole TTL or hand a restore a deployment lease nobody holds.
That is the same reasoning as `serve.lock`, and the first version of this feature got
it wrong: the maintenance lease was archived and the round-trip test saw two files.

What is still left to the operator: the copy itself. A JSONL tar can still capture a
torn last line written *between* the lease check and the read, and the SQLite store
must be copied with `VACUUM INTO` (WAL holds committed rows not yet in the main file).
The lease removes the *racing writer*, not the need for a store-aware copy.

## P30 — a single replica, measured (2026-10-25)

M26 shipped a single-replica SLO with nothing behind it but a promise: `/readyz`, a drain, a
`forgepilot_ready` gauge, and alert thresholds measured against *turn durations in session
logs*. `tools/load_single_replica.py` is the load. It starts one replica (or drives a running
one), spreads turns across **one session per worker** — a second `POST` on a busy session is
`409` by design, so loading one session measures the busy flag, not the server — and prints
nearest-rank percentiles, because an interpolated p95 prints a latency nobody experienced.

**The model is deliberately not in the loop.** With the default profile the `llm` row points at
a real endpoint, and a turn then costs 1.4 s of somebody else's latency plus money. The driver
overrides `llm` with `bundles/llm_echo/` (a zero-latency provider, zero usage) unless
`--real-model` is passed, so the numbers below are *this server's* cost. Both tables are the
same machine on the same afternoon:

| concurrency | turn p50 / p95 / p99 | read p50 / p95 | `/metrics` p50 / p95 | wall | rate |
|---|---|---|---|---|---|
| 1 | 17 / 22 / 88 ms | 4 / 5 ms | 4 / 4 ms | 0.6 s | 97 req/s |
| 8 | 54 / 103 / 193 ms | 20 / 37 ms | 22 / 39 ms | 2.3 s | 216 req/s |
| 24 | 180 / 529 / 803 ms | 78 / 406 ms | 76 / 329 ms | 10.1 s | 145 req/s |
| 48 | 503 / 560 / 610 ms | 148 / 327 ms | 148 / 160 ms | 16.5 s | 178 req/s |
| 96 | 1047 / 1191 / 1323 ms | 300 / 347 ms | 301 / 316 ms | 33.9 s | 173 req/s |

and the same driver against the deployment's own model (`--real-model`, `deepseek-flash`):

| concurrency | turn p50 / p95 |
|---|---|
| 1 | 2 240 / 5 899 ms |
| 8 | 3 090 / 7 763 ms |
| 24 | 3 454 / 8 868 ms |

What the numbers say, and what they do not:

- **There is no cliff below 96 concurrent turns.** Zero failures at every level; turn latency
  grows roughly linearly past 8, which is a queue, not a wall. Throughput plateaus around
  175 req/s — that plateau is the process's own CPU (JSONL appends, projections, the poll loop),
  and finding the exact knee needs a profiler, not a bigger number here.
- **Reads are cheap and `/metrics` is cheaper**, but both degrade together with turns: one event
  loop serves all three.
- **The turn numbers include the driver's own polling.** `POST` answers `202` and the turn runs
  in the background; this client waits by `GET /sessions/{id}` every 5 ms, which is itself load.
  A real client streams `/events` instead, so treat the high-concurrency turn numbers as an
  upper bound on latency and a lower bound on achievable rate.
- **It does not measure a fleet.** One process, one core's worth of event loop; P16's replicas
  are a separate question and this tool says nothing about them.
- **It is not the deployment.** localhost, no TLS terminator, no reverse proxy, replay-free but
  also disk-warm; a Caddy hop and a cold page cache both add milliseconds this table omits.

The tool sets FORGEPILOT_SIGNING_KEY in the child it starts, and that is not boilerplate: a
deployment with no signing key issues **unsigned** credentials and uthenticate — correctly —
refuses to open a scope from one. The first CI run of this tool failed with 401 Unauthorized
while the same test passed here, because this machines .env.local supplies the key and CI does
not. A test that leans on the developers key is a test of the developers machine.


