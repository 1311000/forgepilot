# The API boundary and the workbench

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before touching `httpapi/`, the SSE stream, or the workbench page**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## M10 notes — the API boundary and the workbench

The HTTP layer adds no business logic. All three modes already ran; M10 is a transport over them, and the rule that keeps it honest is that **the stream is the log** — not a summary of it, not a parallel event bus.

Five things are load-bearing, and four of them were found the hard way:

- **A turn must never run inside its SSE handler.** Approval blocks on a future that a *different* HTTP request resolves. A turn holding the connection it is streaming to could never be told "yes" — a self-deadlock, and it presents as a hang rather than an error. So `POST /sessions/{id}/messages` → **202** and a background task; `POST /approvals/{id}` resolves a pending future.
- **`approval/asked` is flushed before the answerer is awaited** (`runtime/tools/pipeline.py`). It was not, and `tool/call` already was — an asymmetry nobody had hit because nothing read the log while a prompt was outstanding. Without it the prompt is invisible, nobody answers, and the turn hangs silently. Pinned by `test_the_prompt_is_durable_while_the_human_is_being_asked`, which reads from the **store**; asserting against `session.events` would pass on the buffer and prove nothing.
- **`Last-Event-ID` is the log's `seq`.** No new mechanism: the store's `read(after=)` already took exactly that cursor. Ordering is therefore the log's, which is why the browser sees `approval/asked` before the tool body — the M4 property (`arm` strictly after approval) is now visible on the wire, not just inside the process.
- **Only durable events are streamed.** Model token deltas are deliberately absent: they are the one thing the log does not hold, and a stream carrying both makes "is this history?" unanswerable from the client. The cost is that a long model step looks like silence until its `assistant/message` lands; the compensation is that `step/start` arrives immediately, so the UI can say "working" instead of appearing hung. **This is the fallback the M10 plan named as R1** — a second, clearly-labelled ephemeral channel is the alternative if the latency ever proves intolerable. It would need either a `tap` threaded through `run_turn`/`run_step` or a transport callback on `Session`, and neither is built.
- **`Session.subscribe` fires on append, before the flush.** Deliberate: an appended event has already been decided, and waiting for durability would lag the reader by a whole step. It also means a listener can see an event a crash then loses — fine, because the *log* remains the truth and a reconnecting reader rebuilds from it. Listeners run **inline on the writing task**, so the SSE bridge hands each event to a queue rather than writing to a socket; a slow client overflows its bounded queue and gets `LostStream`, which closes the stream so the client reconnects and replays the gap exactly. A stream with a silent hole would be worse.

Two traps worth knowing before touching this code:

- **`httpx.ASGITransport` buffers the whole response body before returning.** An endless SSE stream can never be read through it — the request just hangs, and the hang looks exactly like a broken endpoint. `tests/httpapi/` therefore runs a real `uvicorn` on an ephemeral loopback port for anything that streams; `api` (in-process) is for everything else.
- **Never hand the ASGI app out through a service method.** `cordis_port` wraps a service's methods when they are reached through the proxy it returns (`utils.create_shadow_method`), and that wrapper is a synchronous `def`. uvicorn decides ASGI 2 vs 3 by probing `iscoroutinefunction(app.__call__)`; the wrapper fails that probe, so FastAPI gets called as `app(scope)` and every request fails with `missing 2 required positional arguments` — on a server that started perfectly. `HttpApiPlugin` therefore carries **configuration only** (plain data crosses the proxy untouched) and the app is built by the caller with `build_app(root, ...)`. `test_the_plugin_exposes_config_but_never_hands_out_the_app` is the reminder.

Also:

- **`HttpApiPlugin.inject` is declared, not discovered.** An undeclared read on a plugin's own context raises on the first request; on the *root* context it returns `None`. Declaring the dependencies is also what makes the plugin fail closed — without an approval service it stays PENDING rather than serving an API whose prompts could not be routed.
- **The project policy is required, and omitting it is not a way to avoid restricting anyone.** `derive` reads a missing policy as the *most restrictive* one, so the failure mode of forgetting is "every tool call refused", not "unbounded agent". With no `project.yaml` the server falls back to an open policy and **says so on stderr** — a permissive default nobody is told about is how a demo configuration reaches production.
- **`SessionRegistry` is in-process state**, so the API is single-process. Two replicas would each hold half the sessions and neither could stream the other's. Sticky routing or a real pub/sub is the answer; neither is built, and this is not pretend-supported.
- **The workbench does not use `EventSource`.** It cannot set an `Authorization` header, and the usual workaround is a token in the query string — which lands in access logs, browser history and `Referer` headers. The page reads frames with `fetch` + `ReadableStream` instead, which also lets it send `Last-Event-ID` as a header. The refusal of query credentials is pinned by a test.
- **A prompt outlives a restart; its answer does not.** After a restart the log still shows `approval/asked`, but nothing is waiting — `POST /approvals/{id}` returns `answered: false`, and the page says so. A silent success there would tell an operator a tool call was approved when there was nothing left to approve.
- **The binding is a security decision, and `--host` used to make it silently (P4-8).** The default is `127.0.0.1` and the credential is a bearer header, so the design's safety rested on *nothing else being able to reach it* — which is exactly what `--host 0.0.0.0` removes, while saying nothing at all. The workbench even refuses a query-string token so the credential cannot leak through a `Referer`; none of that matters once the port is on the network. `_serve` now warns on any non-loopback bind, naming the plaintext credential, the absence of TLS, and the session log. ⚠️ **A warning rather than a refusal, deliberately**: a deployment behind a TLS proxy on a trusted network is legitimate, and refusing would be the CLI inventing a policy nobody decided — the same choice `--allow-uncontained-exec` makes. A **hostname** that is not `localhost` counts as exposed, because this cannot resolve it and guessing in the reassuring direction is the wrong way to be wrong. `tests/cli/test_serve_binding.py` pins both halves, including that the warning goes to stderr — a warning on stdout is one nobody reads.

## M18 — task mode reaches the API, and the reading comes from the log

Task mode had one entry point (`--task`), so the workbench could show a chat and a review and not the loop the platform exists for. Three decisions carried M18, and each is a rule rather than an implementation detail:

- **A task is a session.** `POST /tasks` returns `{taskId, sessionId}` where the two strings are equal — there is no second identity to keep in sync, no second log, no join table. A task's progress is `GET /sessions/{id}/events`, its approval is the same `POST /approvals/{id}`, and the state the panel shows is `GET /tasks/{id}`, which **folds the same log**. Anything else would be a second source of truth that can disagree with the one the evals read.
- **The state is read back, not remembered.** The registry is process memory, so `GET /tasks/{id}` asks it first and then falls back to the durable store: the session header carries the `principal` (that is what makes "is this yours?" answerable after a restart) and the `task/state` events carry the deliveries. What the log could not say, M18 made it say — each delivery's closing event now carries `commit` and `changed`, and `run_task` writes one final `task/state` with no `repo`: the verdict itself. Before that, `DELIVERED`/`READY`/`FAILED` existed only in the return value, so a reader of the log could not tell a finished task from a stopped one. `interrupted` is exactly that difference: nobody is running it, and the log still says `running`.
- **The runner is not in the route.** `httpapi/tasks.py` owns the background turn; `routes.py` translates. `tests/contract/test_the_api_is_not_a_second_runner.py` fails the gate if any other module under `httpapi/` even names `plan_task`/`run_task`, because a second execution path silently diverges from the one `evals/` measures.

Two smaller things, both consequences of reusing what exists:

- **The ticket is fetched before the session exists.** An unknown id answers 404 and leaves no session behind — a log holding one message from the human and nothing else is worse than no log. `plan_task` takes no session, so the `awaiting_info` refusal (preprocessing decided a human must complete the ticket) is written by the service; without that event the panel would spin on a task that had already decided it could not start.
- **`TaskService.start` refuses without a project.** `build_app` still falls back to an open policy when no `project.yaml` is found, because a chat that cannot run is worse than a permissive one that says so; task mode must not inherit that fallback, so `has_project()` is checked separately and `POST /tasks` answers 409. The page's task strip is refreshed by `task/state` frames rather than a timer, for the same reason the strip exists at all: the log is the truth, and the strip is a reading of it.

`SessionSummary` carried no `principal`, so `GET /tasks` (the list) was deliberately **this process's live tasks only** — an unfiltered list would have shown other tenants' ids, and a filtered one had nothing to filter on. That held until the fix below.

## The listing, and the field that had no reader

The paragraph above was true for two eras and it named its own cause. `SessionSummary.principal` is now carried by both stores, so `GET /tasks` answers from the log: live tasks first (the registry also has their buffered events), then the durable ones, each scoped to the caller's principal.

Three things are worth keeping, because the shape recurs:

- **`SessionStore.summaries` had no caller anywhere in `src/`.** Two implementations and a Protocol entry, and a route that explained why it could not be *used* — the "a field with no consumer because the consumer does not exist is a gap" shape from `defect-shape.md`, except that here the gap and the reason it was invisible were the same sentence. The fix gave it its first reader rather than a second correction.
- **The order is a contract now.** Both stores return **newest first**, stated on the Protocol. The durable store sorts headers by `created_at` (a read of every header — it keeps no time index), because paginating its directory would group by tenant first and hand the first page the alphabetically-first tenant's oldest sessions. Newest-first is what a listing is for; leaving it to storage would push a sort into every caller, which is how a method ends up with none.
- **A listing may not read every session on disk.** A summary carries no events, so telling a task from a chat means opening the log; `DURABLE_SCAN = 50` bounds how many summaries a page will open, and a chat session that happens to be in that window is filtered out by having no `task/state` event rather than by a flag that would need to be kept true.

## M20 — the process became stoppable and countable

Three things about the *process* rather than the API, and each was a real
behaviour a browser or an orchestrator would have hit:

- **A stream must end when the server goes away.** uvicorn waits for in-flight
  connections before it runs the lifespan shutdown, and an SSE stream has no
  reason to finish — so a workbench tab turned `Ctrl-C` into a hang for the
  graceful-shutdown timeout, which is unbounded by default. The signal handler
  now raises the registry's stop flag itself (`cli/main.py::_end_streams_on_signals`),
  the SSE tail races `queue.get()` against that event, and the client reconnects
  with `Last-Event-ID` because the cursor stays valid. A refused turn answers
  **503** (not 409: nothing is busy, another replica could accept), and a waiting
  approval is released as `UNAVAILABLE` so the turn records its ordinary refusal.
- **`/metrics` is aggregate-only, and that is why it needs no credential.** Labels
  are route templates, so a scanner probing `/.env`, `/nope-1`, `/nope-2` lands in
  one `unmatched` series; the gauges (`sessions_live`, `turns_running`,
  `approvals_pending`) are read at scrape time from the registry that owns the
  truth, because a pushed gauge is the one that silently stops falling. No
  histogram: buckets should come from measured traffic, and token counts already
  live in the session log.
- **One server per state directory.** The registry is process memory, so a second
  process would hold half the sessions; `runtime/serve_lock.py` refuses it, with
  the path and the pid in the message. `deploy/compose.yaml` therefore has no
  `replicas` key on `api` — scaling past one process is a design change (shared
  registry and a bus), not a flag.

The container, the proxy and the environment table are in `deployment.md`.


