# Runbook

One page per alert in [`deploy/alerts.yml`](../deploy/alerts.yml), and two procedures that no
alert watches because nothing would fire for them in time.

This is a **record**, not a rule: `CLAUDE.md` states what must stay true, and these pages say
what to do when one of them stops being true at 3 a.m.

**Why this file exists.** An alert without a page is a fact somebody has to invent a response to
at 3 a.m. Each page below answers five questions in the same order, so a reader under pressure
reads the same shape every time: what it means, the first three commands, what "fixed" looks like,
who to wake, and — the one people skip — what this alert **cannot** tell you.

The two guards keep this file honest:
`tests/contract/test_every_alert_has_a_runbook_page.py` fails if an alert has no page or a page
names an alert that does not exist, and `tests/contract/test_the_alert_thresholds_are_measured.py`
fails if a `for:` value is not in the table below or if the two thresholds that must sit on a
particular side of a code constant stop doing so.

## Where the thresholds come from

Nothing here was chosen because a number looked round. Each one is measured on this box or
derived from a constant in the code, and the derivation is what the third column records.

| alert | `for:` | derivation |
|---|---|---|
| ForgePilotDown | 2m | no measurement; two default (60 s) scrape intervals |
| ForgePilotDraining | 1m | 4 × `DEFAULT_SHUTDOWN_TIMEOUT_S` (15 s); the drain test measured a 5.2 s stream |
| ForgePilotNoScrape | 5m | no measurement; five default scrape intervals |
| ForgePilotApprovalWaiting | 4m | below `DEFAULT_APPROVAL_TIMEOUT_S` (300 s), the timeout it has to catch |
| ForgePilotTurnStall | 10m | 1343 live turns: p50 0.2 s, p95 28.0 s, max 242.5 s |

The turn distribution comes from the durable session logs on this box — every
`turn/start` paired with its `turn/end` (n = 1343, all `deepseek-flash`). No approval was ever
answered in a log here, so that row has no distribution and its threshold is derived from the
timeout instead of invented: the alert must fire *before* the request expires, or it reports
history.

## Alerts

### ForgePilotDown

**What it means.** `up{job="forgepilot"} == 0` for two minutes: Prometheus cannot scrape the
process at all. One process owns the state directory (`runtime/serve_lock.py`), so this is the
whole service — there is no second replica to take over.

**First three commands.**
```bash
docker compose -f deploy/compose.yaml ps
docker logs --tail 80 forgepilot
ls -l "$FORGEPILOT_HOME/serve.lock" && cat "$FORGEPILOT_HOME/serve.lock"
```

**What "fixed" looks like.** `GET /healthz` returns 200 (unauthenticated, no database), and
`forgepilot_ready` is 1 on the next scrape. A restart that exits **2** immediately means the
lock file is held by another process — read the pid in it before deleting anything.

**Who to wake.** The on-call for the deployment. If the container exits 2 and the pid in the lock
is not running, that is a stale lock: delete it and start again, and record why it was stale.

**What it cannot tell you.** Whether the *work* is healthy. A service that is up and refusing
every turn looks identical from here.

### ForgePilotDraining

**What it means.** `max(forgepilot_ready) < 1` for a minute. `/readyz` returns 503 while a
shutdown is in progress (`registry.stop()` flips the same flag), and in-flight turns finish
before the process leaves. A minute is four times `DEFAULT_SHUTDOWN_TIMEOUT_S`, so this means a
shutdown that is not finishing.

**First three commands.**
```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/readyz
curl -sS http://127.0.0.1:8000/metrics | grep -E 'turns_running|sessions_live'
docker logs --tail 40 forgepilot
```

**What "fixed" looks like.** Either the process exits (the normal case) or `/readyz` returns 200
again because the shutdown was cancelled. New `POST /sessions/{id}/messages` requests returning
503 while `/healthz` is 200 is the expected state *during* a drain — not a fault.

**Who to wake.** The on-call, but only if it lasts past a further minute; a drain that completes
in seconds is the mechanism working.

**What it cannot tell you.** Where the blocked stream is. The log names the session id; the turn
detail is in that session's JSONL.

### ForgePilotNoScrape

**What it means.** `absent(forgepilot_ready)` for five minutes: the process may answer `/healthz`,
but `/metrics` is not being scraped — a proxy route or a port mapping, not the application.

**First three commands.**
```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/metrics
docker compose -f deploy/compose.yaml port forgepilot 8000
curl -sS http://127.0.0.1:8000/metrics | head -5
```

**What "fixed" looks like.** A scrape returns the exposition text (`text/plain; version=0.0.4`),
and `up{job="forgepilot"}` returns to 1.

**Who to wake.** Whoever owns the reverse proxy (`deploy/Caddyfile`), not the application owner.

**What it cannot tell you.** Whether the service is serving turns; this rule reads one endpoint's
reachability.

### ForgePilotApprovalWaiting

**What it means.** `forgepilot_approvals_pending > 0` for four minutes. A turn is blocked on a
human, and the request expires at five minutes (`DEFAULT_APPROVAL_TIMEOUT_S = 300.0`) — after
which the tool call is refused with `UNAVAILABLE`.

**First three commands.**
```bash
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/sessions | head -40
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/sessions/$SESSION_ID | head -60
curl -sS -H "Authorization: Bearer $TOKEN" -X POST \
  -H 'content-type: application/json' \
  -d '{"decision": "allowed-once"}' \
  http://127.0.0.1:8000/approvals/$REQUEST_ID
```

**What "fixed" looks like.** The pending approval is answered (`allowed-once` or `rejected`) and
`forgepilot_approvals_pending` drops to 0. Letting it expire is also a *resolution* — the turn
gets a refusal — but it is not a fix, it is a decision made by the clock.

**Who to wake.** The person the ticket was assigned to. The request id and the tool call are on
the session page.

**What it cannot tell you.** Whether the agent's proposal is any good. The page shows what it
wants to run; judging it is the human's job, which is why the approval exists.

### ForgePilotTurnStall

**What it means.** `forgepilot_turns_running > 0` for ten minutes. The longest live turn measured
on this box took four minutes, so a turn this old will not finish on its own: a command is
waiting on something (the 2026-10-19 eval hang was a child process reading stdin).

**First three commands.**
```bash
curl -sS http://127.0.0.1:8000/metrics | grep -E 'turns_running|sessions_live'
curl -sS -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/sessions/$SESSION_ID/events?after=0" | tail -20
docker logs --tail 60 forgepilot
```

**What "fixed" looks like.** `forgepilot_turns_running` returns to 0. If the last event is a
`tool/call` with no matching `tool/result`, that call is the one to kill; the turn then records a
failure rather than finishing.

**Who to wake.** The on-call. A stall that survives the sandbox timeout is a defect in the
executor, not in the ticket.

**What it cannot tell you.** Whether the turn is *useful* — a long turn doing real work is
indistinguishable from a long turn waiting on stdin by the gauge alone. The session log's last
event is what separates them.

#### A session that is running on another replica

`GET /tasks/{id}` says `running` while this replica has no such session in
memory: the log is telling the truth and this process is not the writer. Confirm
with `GET /sessions/{id}` — if it answers with `"running": false` and a non-zero
event count, the read fell back to storage and the *other* replica holds the lease.

`ForgePilotTurnStall` and `ForgePilotApprovalWaiting` fire on the **sum** across
replicas, so the page does not say which one; `forgepilot_turns_running` on each
replica's `/metrics` does. `ForgePilotDown` and the draining page are per target.

The only safe actions from this replica are reads and answering an approval the
log shows as pending (`POST /approvals/{id}` works from any replica). Do **not**
`docker restart` this one hoping to "clear" the session: it holds no state, and
the writer is unaffected. To stop a run that has gone wrong you must reach the
replica holding the lease, or wait for the lease to expire — an expired lease is
taken over safely, with `fencing` incremented.

## Procedures no alert watches

#### Backup and restore, once per release

```bash
python -m forgepilot --config-dir configs --backup /var/lib/forgepilot/saved.tar.gz
python -m forgepilot --config-dir configs --restore /var/lib/forgepilot/saved.tar.gz --overwrite
```

Both refuse while a server holds the state directory (that is `runtime/serve_lock.py`, the same
lock `ForgePilotDown` points at). The archive carries a manifest with each file's size and
SHA-256; a restore verifies them in a staging directory **before** it replaces anything, so a
corrupt backup leaves the current state untouched. Restore into a *second* directory and start a
server against it if you want to see the data before trusting it.

#### The serve lock

`$FORGEPILOT_HOME/serve.lock` holds the owner's pid at offset 0 and a locked byte at offset 4096.
A second server exits **2** and says which pid holds it. Never delete the file while that process
is alive: two processes on one state directory interleave their JSONL appends and the second one
wins the file cache. If the pid is gone, delete it and start again.

## Backing up while a fleet is running

`--backup` and a `--multi-replica` server do not exclude each other. If the state
directory holds `sessions.db`, do not tar it: use `sqlite3 sessions.db "VACUUM INTO
'backup.db'"`, which is safe while writers are active. For JSONL state, prefer a
window with no traffic; a torn tail is caught by restore, but the archive is then
useless.
