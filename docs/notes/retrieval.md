# Retrieval

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before changing the retriever or adding a retrieval provider**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## Retrieval notes (read before changing the retriever or adding a provider)

`providers/retrieve/grep.py` replaced a mock that returned canned hits. Until it
landed, **nothing in this system had ever searched a file** — the retriever seam
had a fake on one side and no consumer on the other.

- **Ripgrep, with no fallback scanner.** A Python reimplementation would be a
  *different search* — different regex dialect, different glob semantics,
  different handling of binary files and `.gitignore` — and a caller would have no
  way to tell which one produced a hit. That is the same argument as refusing to
  present a text match and an AST parse as the same evidence; two text searches
  are no different. When the binary is missing the provider says so and names the
  install.
- **The binary is resolved in three places**, and the third is the one that
  matters: an explicit `binary:` in config, `PATH`, then **beside the running
  interpreter**. A deployment that installs ripgrep as a Python package gets it in
  the virtualenv's `Scripts`/`bin`, which is on `PATH` only while the venv is
  *activated* — and every command in this repository invokes the interpreter
  directly, which is exactly when it is not. Claude Code goes further and embeds
  ripgrep in its own binary; this is the same idea with less machinery.
- **`ripgrep-bin` is a `dev` dependency**, so the tests exercise the real binary
  rather than a stand-in. A deployment installs ripgrep its own way.
- **The ref on a hit is resolved, not echoed.** Reporting back the ref the caller
  asked for would claim the results came from that revision — for a working-tree
  search, exactly the false confidence the seam warns about. The commit is read
  from the checkout (`rev-parse HEAD`), uncommitted edits make it `<sha>+dirty`,
  and a directory that is not a repository reports `WORKTREE`: no revision exists,
  so none is invented.
- **A repository with no configured checkout raises** (`UnknownCheckout`) instead
  of returning no hits. `()` for "I searched nothing" is indistinguishable from
  "there was nothing", and a caller concludes the symbol does not exist.
- **Paths are normalised to `/`, platform separator only.** Ripgrep emits
  `.\src\login.py` on Windows, and a hit whose path is backslash-separated matches
  no diff, no group and no changed path — it reads as a file that does not exist.
  Only `os.sep` is rewritten, because `\` is a legal filename character on Linux
  and rewriting it there would silently rename files.
- **There is no disabled row for the enterprise provider, and there used to be.**
  `retriever-remote` pointed at `forgepilot.providers.retrieve.remote_index` — a
  module that does not exist. Nobody noticed because `--dump-config` never imports
  (by design) and `--boot` skips disabled rows. **A row naming a plugin nobody
  implemented is a claim, not a placeholder.** Adding one is a later layer
  overriding the `retriever` row id; the contract is `seams/retriever.py`, and the
  three things a provider must get right are its `capabilities()`, the ref on
  every hit, and re-checking `ExecutionConstraints` itself rather than trusting
  its caller.

### Decision: a fake enterprise provider first, the real one later

The target design makes the remote index **load-bearing** (R6: 300+GB over GVFS,
local scan is not merely slow but harmful). It is also the one item in the plan
that **cannot be verified on this machine**, because it needs a service that does
not exist here — an inverted index plus a vector index, or the company's existing
enterprise search behind an adapter.

**So it is deferred, and what lands in the meantime is a *fake* provider** that
implements `seams/retriever.py` honestly and is wired by overriding the
`retriever` row. ⚠️ **This is deliberately not the deleted `retriever-remote`
row.** That row was a *claim* — a module nobody wrote — whereas a fake provider is
a real module with real behaviour and a contract test, which is exactly what makes
it distinguishable from a placeholder. The distinction matters because the two
look identical in `--dump-config`, and that is how the first one survived.

What the fake provider is for, and what it must not pretend to be:

- **It makes the seam testable end to end** without the enterprise service: a
  second provider behind the same Protocol is what proves the interface is
  genuinely swappable rather than shaped around ripgrep.
- **It carries the ref on every hit** and re-checks `ExecutionConstraints` itself
  — the two things the seam demands of *any* provider, and the two a fake is most
  likely to skip "because it is only a fake".
- **It must not claim `capabilities()` it does not have.** A fake answering
  `remote_index` would let a composition decide a *capability* had been provided.
- ⚠️ **It does not resolve the premise.** Until the real provider exists,
  `README.md`'s「跨仓」and the GVFS scale story remain served by the seam and not
  by the runner. That is the honest state, and it is the same sentence the
  multi-repository note in `docs/notes/task-delivery.md` ends with.

**Update: the provider landed** — `providers/retrieve/index.py`
(`CodeIndexRetrieverPlugin`), with `tests/providers/test_code_index_retriever.py` pinning the
four things the decision names. Two things were settled while writing it:

- **`supports_remote=False`, and that is a decision rather than a default.** The provider
  genuinely serves content from outside the checkout, so `True` is arguable; the reading here
  is that "remote" names *the shared enterprise index that does not exist on this machine*,
  and a fake answering `True` would let a composition decide that capability had been
  provided — the one thing the decision forbids a fake from doing. Confirmed with the operator
  rather than assumed.
- **A missing or malformed index raises**, naming the file and the entry. "No hits" from a
  provider that never loaded is indistinguishable from "there is nothing there", which is the
  same argument as an empty ticket directory raising instead of returning `()`.

⚠️ **Two steps remain, and they belong together.** (1) A bundle or example patch that
overrides the `retriever` row — composition, not code. (2) **An eval task whose answer is only
in the index**, which is the shape that finally prices retrieval: on a local checkout
`find-the-rule`'s own docstring already records that `findstr /s` does the same job as
`code_search`, so no local task can measure the difference.

Doing (2) needs a task-level way to add a patch row — `EvalTask` has `project` (a
`project.yaml` override) but no `patches`, while `Arm` has both. ⚠️ **Adding `patches` to
`EvalTask` and then not writing the task is worse than not adding it**: a field with no
consumer is exactly the shape the defect table in `docs/notes/defect-shape.md` is about, so the field and its first
task have to land in the same change.

**`code_search` closes the loop**, and its shape follows Claude Code's Grep tool,
which was arrived at against real use:

- **`output_mode` defaults to `files_with_matches`.** A model handed forty
  matching lines when what it wanted was *which files matter* has spent context on
  something it did not ask for. `content` and `count` are opt-in.
- **A cap carries an explicit `truncated` marker** — returning the first N
  silently reads as "that is all there is", and the model stops looking.
- **The grant is checked before coverage.** Asking about a repository the caller
  may not read and being told "no checkout is configured" would confirm that it is
  *not* there — a probe for which repositories this deployment holds. The same
  reasoning that makes the HTTP layer answer 404 rather than 403 for someone
  else's session.
- **It cannot name a repository it was never shown.** The system prompt lists repo
  *prefixes* (`git.corp/team/*`), so requiring the model to write a slug would
  make it guess. Candidates come from the new `Retriever.repositories()` and the
  *grant* comes from the caller's constraints — the caller narrows, the provider
  supplies.
- **`SearchQuery.ref` is passed as `WORKING_TREE`**, a seam sentinel, not
  `"HEAD"`. An agent editing a checkout has no revision to name, and `"HEAD"`
  would claim a commit while the results include uncommitted edits; each hit
  reports the commit actually read.

⚠️ **`code_search` lives in its own plugin** (`CodeSearchToolPlugin`, bundle row
`tools-search`), because it needs a different service from `read_file`. Folding
both into `BuiltinToolsPlugin` meant a host with no retriever lost *every* tool —
`write_file` included — which is fail-closed in the wrong direction: the point is
that an absent capability should cost you that capability, not the ones you have.
Three separate test rigs failed that way before the split, each with the confusing
symptom `no such tool: 'read_file'` in a suite that has nothing to do with search.

## M19 — `read_code`, and a corpus that is not a checkout

`Retriever.open` was made correct at M14 (it reads the copy the search saw) and had **no
caller in `src/`**. That residual is now closed by a consumer rather than by another
correction: `read_code` (`build_search_definitions`, `Operation.READ_CODE`, effects READ)
is an agent's way to read a file by **repository path and revision**, with the content
possibly never checked out.

What the tool adds, and why each part is not decoration:

- **The header names the revision that was served.** `read_code` asks for `WORKING_TREE`
  by default (an agent editing a checkout has no revision to name), and a retriever may
  answer from `main`. A slice that does not say which revision it came from is the wrong
  version of a file, delivered confidently — the failure mode the seam's `ref_to_serve`
  rule was written for.
- **`root` is the session's working tree**, exactly as `code_search` passes it. Reading
  the configured checkout instead answers "what did I just edit?" with pre-edit content.
- **The repository is chosen from the permitted set**, and an unknown slug gets the same
  sentence as a forbidden one. "Does this deployment have that repository?" is a probe
  `read_code` does not answer, for the same reason the API answers 404 for someone else's
  session.
- **The default span is 80 lines.** A model that wanted "this file" and received 2 000 has
  spent its context on a listing; one that wanted a function and received 10 cannot see
  its guard clause.
- **`_render_hits` now carries the same two facts** — the repository when more than one is
  in play, and a `[served from: repo@ref]` line. It was dropping both, so a hit the model
  found through an index was a dead end: an unaddressable path, with the one field it
  needed to call `read_code` missing from the answer.

⚠️ **The index provider's `open` had to follow the same labelling rule as its `search`.**
An index holds committed revisions and cannot serve `<worktree>`, while `read_code` asks
for the working tree by default; an exact-match `open` would answer "the index has no
such file" about a file it is holding — the one answer a caller cannot distinguish from
absence. It now resolves through `ref_to_serve` and returns the ref it served.

**Composition, not code**: `providers/retrieve/index.py` existed and no bundle referenced
it, so it was test-only. `src/forgepilot/bundles/code_index/patch.yaml` overrides the
`retriever` row by id — same seam, different reach, `code_search`/`read_code` unchanged —
and `tools/build_code_index.py` turns a real checkout into its corpus (the provider's own
fixture is nine lines, and a fixture is not a deployment). Two things the builder does
that a naive one would not: it records `git rev-parse HEAD` plus `+dirty` rather than a
branch name, and it refuses a `--root` that is merely *inside* a repository — `git -C
<subdir> rev-parse` walks up, so a subdirectory would be labelled with the whole
repository's revision while holding a corner of it.

⚠️ **Two claims in this paragraph were true when it was written and are not now, and the
paragraph is kept as the record of that.** It said (1) that there was no composite
provider — there is one, below — and (2) that the eval task whose answer is only in the
index was unwritten — `evals/tasks/not-in-the-checkout/` has had it since M19/M21, with a
held-out probe for the exact contract, and `task.yaml:35-40` is the `patches` row that
points `retriever` at the index. That is the same defect this table records elsewhere: a
sentence with no reader keeps asserting a gap long after the code closed it.

⚠️ **What is still missing, and must not be claimed from either bundle.** The **shared
enterprise index** — server-side, incremental per commit, shared across developers — does
not exist and cannot be verified on this machine. What exists is a file one deployment
lays down (`tools/build_code_index.py`), read whole and searched in memory. `supports_remote:
false` is the honest capability for both the index and the composite.

## P15 — the index learns about a new commit (2026-10-21)

An index that is only ever built once is a corpus with a timestamp nobody checks. P15 adds
the write side, and the two decisions that make it honest:

- **Read and write are different seams.** `IndexService` (search/symbols/open/status) stays
  what a consumer holds. `IndexWriter.apply(IndexUpdate, constraints)` is what an ingestion
  job holds — a token allowed to read a repository has not thereby been granted the right to
  replace what everyone else is served, and the reference service refuses writes unless a
  token is named with `--write-token`.
- **A snapshot and a delta are different statements.** `IndexUpdate.complete=True` means
  "this is the whole repository at this ref", so anything the index holds that is not named
  is removed. `complete=False` is a delta: documents are upserted, `deletes` are removed,
  everything else stays. A service that guessed between them would either delete a
  repository or keep a file that no longer exists.

**Staleness is a function, not a sentence.** `lag(status, repo, current_ref)` returns
`behind/indexed_ref/current_ref/detail`, and a status that reports **no** per-repository ref
is `behind=True`. "The index did not say" and "the index is current" are different facts, and
only one of them justifies trusting an answer. `GET /status` now returns `refsByRepo`; the
HTTP client parses it into `IndexStatus.refs_by_repo`.

**What is still missing, stated rather than implied**: there is no `IndexWriter` *client* in
`src/` — the writer seam has one implementation (the reference service's `POST /index`) and
its test posts over HTTP directly. `lag()` has no caller in `src/` yet either: the consumer
that would use it is the one that knows the checkout's `HEAD`, which today is the retriever
provider, not the index client. Both are gaps, not defects: the reader does not exist.

## M24 — the corpus, with the tree in front of it

`providers/retrieve/composite.py` (`CompositeRetrieverPlugin`, bundle
`bundles/code_index_working_tree/patch.yaml`) is the composition the target design
describes and the one the two existing providers could not express. It is composed by
**inheritance from the local provider** and by **holding an `IndexCorpus`** rather than by
instantiating a second plugin: `cordis_port.Service.__init__` registers the provide row, so
a provider that built another `Service` inside itself would provide `retriever` twice.

Three rules, each of which is a way the composition can be convenient and wrong:

- **A file that exists on disk is answered from disk.** The corpus copy of that path is
  stale by definition — same path, older content — and serving it produces exactly the
  confidently wrong answer `CodeRef.root` was added for at M14.
- **Deleted is not absent.** A path in `HEAD` but missing from disk is a deletion in
  progress: `search` drops it, `symbols` drops it, and `open` refuses it *and says which of
  the two it is*, because "the corpus has no such file" would be false.
- **Every hit names its revision**, and the two halves name different ones:
  `WORKING_TREE`-relative hits report the commit actually read (with `+dirty` when the tree
  is not clean), corpus hits report the revision the corpus holds.

`capabilities().covers_working_tree` is `bool(roots)`, not `True`: a deployment whose
`roots` is empty has a corpus and nothing else — the GVFS shape — and telling a caller its
edits are visible when nothing is looking at them is the overclaim the field exists to
prevent.

**The eval task that prices it**: `evals/tasks/stale-corpus/` points the composite at an
index holding a **draft of `limits.py` itself** (`MAX_PER_PAGE = 500`) while the checkout's
branch says 250. The visible test asserts a band that excludes 500; the held-out probe goes
through the contract's guard and pins the clamp to the tree's number. Measured 2026-10-19:
the correct fix passes, and the `trusts-the-corpus` variant — which passes every visible
check — fails on the probe with `enforce_page_size(5000) is 500; it must clamp to this
checkout's MAX_PER_PAGE (250)`. Offline, all eight arms pass (the scripted model writes the
answer), so the arm that separates composite from index-only is a **live** measurement, and
this note must not be read as one.

## M28 — `read_code` was offered to nobody, and the arm that found it (2026-10-19)

The tool M19 wrote was built, tested and documented, and **no `tool_sets` entry anywhere
named it** — `examples/stat-fix/project.yaml:30` and `patch.yaml:89` mention it only in
`allowed_operations`, which is a permission, not an offer. `orchestration/router.py` hands
the model the names a tool set names, so in every real deployment the tool existed and
could not be called. The fix is four lines in two project files; the guard is
`tests/contract/test_every_tool_is_offered_by_a_project.py`, which parses every project and
task file and fails both ways — a built tool nobody offers, and an offered name that is not
built. Its first run was red on `'read_code' in offered`, which is the falsifier.

The reason nobody noticed is instructive: every test of `read_code` calls it **directly**,
through the registry, so the whole suite passed with the tool unreachable. The gap was
between the registry and the router, and no test looked at that seam from the configuration
side. It is the same shape as the table in `docs/notes/defect-shape.md`, one level out: not
a field with no reader, but a capability with no configuration that offers it.

**What this means for pricing the composite**: `no-shell` (`evals/arms.py`) is the arm that
removes `run`, and it is what made the missing offer visible, because it had to name every
tool it allows. It did **not** price the composite — under it, `stale-corpus` passes 3/3
with the composite and 3/3 with `index-only`, because `read_file` on a directory lists it and
thus remains a discovery path over a local checkout. Pricing the composite needs a checkout
where reading is itself a retrieval call, which is a deployment property; see
`docs/notes/evals.md`.

## Q4 — the shared index is an interface now, not a paragraph (2026-10-20)

The target environment in `ForgePilot文档.md` is **remote-index-first**: 300+ GB across
repositories, a local checkout holding metadata, contents fetched on demand. Until Q4 the
only thing that existed for it was `providers/retrieve/grep.py` (the local overlay) and
`providers/retrieve/index.py` (a JSON file — the *shape* of an answer, not a service). The
enterprise provider was a paragraph in this note.

`src/forgepilot/seams/index_service.py` is that paragraph turned into something a deployment
can satisfy, and `src/forgepilot/providers/retrieve/remote.py` is the client for it. The
provider implements **both** interfaces on purpose: `IndexService` is what a team's existing
search service adapts to, and `Retriever` is what the tools already talk to — so a deployment
enables it by overriding one row (`bundles/retriever_remote/`) and no tool changes.

Three rules, each one a defect this repository has already recorded:

1. **Permission is re-checked at the provider, before the wire.** A repository the caller
   names and may not read is refused rather than quietly dropped from the request; sending
   the question and filtering the answer would let a compromised server answer it. A hit the
   service returns for a repository the caller may not read is dropped anyway, by both the
   adapter and the provider — a server bug must not become a leak.
2. **Every answer carries its revision and the corpus's freshness.** Slices carry
   `indexed_at`; hits carry the `ref` the service served. A slice that does not say which
   revision it came from is the confidently-wrong answer M19 was about.
3. **"I cannot answer" is not "nothing there".** `IndexRefused` (permission), `IndexUnavailable`
   (outage, 5xx, transport failure, non-JSON) and an empty result are three different answers,
   because the first two are fixed by different actions and an empty result is a finding. A
   transport failure says so in its message, so a model cannot read it as "no match".

`covers_working_tree` is `False` and `supports_remote` is `True` — the only provider that may
claim the second, and none of them may claim the first from a shared corpus. The bundle is
**not** in a shipped profile: `tests/contract/test_the_index_seam.py` fails if any profile
people start with loads it, because it sends a bearer token to a service.

⚠️ Still not here: incremental updates and a permission model that survives an attacker with
the service's own database. What Q4 added is the interface and the failure taxonomy; what
**P11** added is a service that actually answers — `tools/serve_index.py`, a reference index
over SQLite FTS5 with `--token NAME=SCOPE`, `/search`, `/symbols`, `/file` and `/status`, whose
bearer scope is checked before the corpus is read. It is a reference, not the enterprise one:
it indexes files an operator hands it, holds one revision per path, and its "incremental
update" is `--index` again.

The client is now tested two ways rather than one: against `httpx.MockTransport` (the wire
shape) and against that process on a real socket
(`tests/providers/test_the_index_over_http.py`). The second found four defects the first could
not, because a mock never serialises a request, never runs a server's event loop, and never
returns a 500:

- `from __future__ import annotations` turned `request: Request` into a string FastAPI could
  not resolve inside `build_app`, so it read the parameter as a **query field** and every POST
  answered `422 missing query parameter request`. The fix is a module-scope alias, and the
  comment beside it is the only reason a reader will not undo it.
- `IndexCorpus.search`/`symbols`/`open` are `async`; the handlers called them synchronously
  and iterated a coroutine (`TypeError: 'coroutine' object is not iterable` → 500).
- `SymbolQuery` takes `kinds` (a tuple), not `kind`; the service passed the singular and every
  `/symbols` call was a 500.
- The test redirected the server's stderr into a `subprocess.PIPE` and never read it. A
  traceback is bigger than a pipe buffer, so the server blocked writing its own error and
  stopped answering: a 500 arrived as a **client timeout**. The error was an answer, and the
  harness had made it unreadable — the same shape as the defects in `docs/notes/defect-shape.md`,
  one layer out.

**A slice that could not come from disk.** The test also asks for
`corpus-only/remote_only.py`, a path that exists nowhere in this repository, and asserts the
service returns its text. Every other assertion in that file could in principle be satisfied by
a provider that quietly read a checkout; that one cannot.

## P20 — a commit becomes a delta, and the client that found two service defects (2026-10-22)

`runtime/index_update.py` turns a commit into an `IndexUpdate` by asking git, not by
re-reading the tree: `git diff --name-status -z --find-renames base..head`, with two
rules that are easy to get wrong and are therefore stated in the module docstring and
pinned by tests. **A rename is two statements** — the old path is deleted and the new
one added, because an add alone leaves the old path answering forever. **A delete is
sent as a delete**, never as an empty document: an empty file and a removed file look
identical to a searcher and only one of them is true. A file that cannot be read as
UTF-8, or is over `MAX_DOCUMENT_BYTES`, is dropped **and named** in `Plan.skipped`;
an ingestion job that silently drops half a commit builds an index that confidently
says "nothing there" about code that exists.

The same trap the index builder hit in M19, and the SWE-bench spike hit twice, appeared
a fourth time: `git -C <plain directory> rev-parse HEAD` walks **up** to the enclosing
repository, so a directory that merely sits inside a checkout answers with a commit it
does not hold. `_assert_checkout_root` compares `git rev-parse --show-toplevel` with the
directory and refuses, which is why `git_head`/`git_delta`/`tracked_files` all refuse a
plain directory rather than returning a neighbouring repository's history.

`HttpIndexWriter` (in `providers/retrieve/remote.py`, beside `HttpIndexService`) is the
first client the `IndexWriter` contract has ever had, and the CLI drives it:

```
python -m forgepilot --index-update git.local/demo/ingest \
  --index-url http://127.0.0.1:8001 --repo-root <checkout>
```

It reads the base from the **service** (`GET /status` → `refsByRepo`), not from anything
this process remembers, because a remembered base is a second source of truth that
disagrees the first time the job runs elsewhere. `None` from `indexed_ref` is not "the
same as HEAD": it means the service has no revision for that repository, so the job
sends a complete snapshot rather than an empty delta that would claim "nothing changed".

**Two defects the client found in the reference service**, both in `_constraints` in
`tools/serve_index.py`. A `*` token's authority was resolved against the *corpus*:

* the first snapshot of a repository the index did not hold yet was **403** — an index
  that refuses to learn about anything it does not already know cannot be ingested;
* once a repository's last file was deleted, its slug left the corpus and the same token
  got **403 "you may not read this"** where the truth is "the index holds nothing for
  it". Those two answers lead to opposite decisions, which is this seam's first rule.

The fix is not a new permission model: `*` becomes the glob prefix `*`, which
`ExecutionConstraints.allows_repo` already understands, so a `*` token covers every
slug and an empty answer is an empty result. Scoped tokens are unchanged.

Not here yet: **nothing schedules the job** — no webhook, no polling loop, so freshness
is "as often as somebody runs `--index-update`". A per-repository partial failure in the
service's `/index` is also unreported (one 200 means every document was stored), and
`IndexWriter` still has no composite/fan-out form for more than one shard.

## P21 — shards, a fan-out, and the shard that did not answer (2026-10-23)

The reference service can now split its corpus two ways — one shard per repository
(`--shard-by repo`, the default) or one per top directory inside a repository
(`--shard-by top-dir`) — ask only the shards a query could match, and answer with bookkeeping
that says which shards were asked, which were skipped, and which never answered.

The measurement, `tools/bench_index.py`, 10 020 files across 20 repositories on one host, one
process, in-memory FTS5, 60 queries per cell:

| layout | shards | query | hits | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| repo | 20 | every shard | 12000 | 68.4 | 70.3 | 75.3 |
| repo | 20 | `globs mod03/**` | 1200 | 47.2 | 51.2 | 57.6 |
| top-dir | 200 | every shard | 12000 | 82.0 | 84.1 | 90.5 |
| top-dir | 200 | `globs mod03/**` | 1200 | 12.0 | 12.8 | 13.5 |

Three readings, and the first one is the honest one:

- **Fan-out is not free.** With every shard asked, 200 shards cost ~14 ms more at p95 than
  20 — the overhead is per shard, not per file. A layout that only ever answers unfiltered
  queries is a layout that should be the coarser one.
- **Selectivity is what sharding buys.** The same narrowed query is 12.8 ms on 200 shards
  against 51.2 ms on 20, because 190 shards are never asked at all. That is the case a
  sharded index exists for, and it is why the shard is `(repository, top directory)` rather
  than a hash of the path.
- **The deadline is the correctness boundary.** Every query carries a per-shard budget
  (`deadlineMs`, default 1200 ms, clamped to 1 ms–30 s). A shard that misses it is
  **reported**, not treated as empty: `shards.failed` names it with the reason, and both
  response models carry `shards: {answered, failed, skipped}`.

The client refuses rather than returns the rest (`HttpIndexService` calls
`_refuse_failed_shards`): `IndexUnavailable: search: 1 shard(s) did not answer —
git.corp/team/mono#web (no answer within 0.150s). This is not an empty result: those shards
could not be asked.` Presenting the answered shards as the answer would make a timeout
indistinguishable from "the code is not there", which is the one confusion the seam forbids.

`tests/providers/test_the_index_fans_out.py` pins the decisions in-process (layout, empty
`repos` selects nothing, glob selection, timeout naming, exception naming, clamped deadline,
client refusal). `tests/providers/test_the_index_is_sharded.py` starts the service on a real
port with `--shard-delay mono#web=0.6 --shard-deadline-ms 150` and proves the whole chain:
the same query is refused when it needs the slow shard, answers when it skips it, and answers
fully when the caller sets a `deadlineMs` it can wait for.

Two defects surfaced while wiring it, both of the shape this project keeps finding:

- **Shard selection and file filtering disagreed about `api/**`.** Shard selection read it as
  a directory (correct); the corpus filter stripped the leading `*` and compared suffixes, so
  `api/handlers.py` was asked for and then dropped — a silent empty answer, not an error.
  `seams/retriever.py::matches_glob` is now the one definition both levels use. This is the
  second time a glob has been read two ways in this provider (`search(root=…)` was the first).
- **A prefix that means two things.** Under `repo` an empty prefix means "the whole
  repository"; under `top-dir` it means "files with no directory". Skipping the root shard
  for `web/**` is exact in one and wrong in the other, so `Shard` carries its `layout` and
  `_shard_could_hold` reads it. The test that caught it asserts the *chosen* set, not just
  that the query answered.

Still not here, unchanged: a real enterprise index (P20's client and P21's shards are the
reference service's, one host, no replication), and a shard set that changes while queries
are in flight — a rebuild swaps the whole list under the lock, which is honest for a
reference implementation and not what a 200-repository deployment should do.


## P22 — a cheap edge: who imports this module (2026-10-22)

A retrieval system that can answer "where is this name" still cannot answer the first
question a change asks: *who depends on this?* P22 adds that, and it adds the cheapest version
of it deliberately.

**What an edge is.** A static ``import``/``require`` statement naming a module, recorded per
file and per revision. ``from astropy.modeling import separable`` records
``astropy.modeling`` and **not** ``astropy.modeling.separable``: whether the last component is
a submodule or a name defined in the package needs the filesystem, and guessing is how a
module graph starts lying. For Python the statements come from ``ast`` (so a comment or a
string that merely contains the word ``import`` is not an edge); for JavaScript/TypeScript and
Go, three regexes with a positional sort, and nothing at all for a language nobody taught it —
an unknown suffix yields no edges, which is not the same claim as "imports nothing".

**What it answers.** ``POST /importers {module, repos}`` over the same wire as the rest, and
``IndexService.importers`` on the seam. Matching is by **whole components**:
``astropy.modeling`` matches ``astropy.modeling`` and ``astropy.modeling.fitting``, never
``astropy.modeling_extra``.

**What changes when a file changes.** An ingested document **replaces** its own edges: a file
sent without an import has stopped importing it, and keeping the old edge would answer with a
file that no longer does. ``deletes`` removes the edges with the file; a ``complete`` snapshot
removes the edges of every path it does not name.

**What it is not.** No call graph, no name resolution, no dynamic imports (``importlib``,
``require(variable)``). A cross-repository question works because the slug is carried on every
edge, but "this function calls that one" is a v2 question and the notes above are why: the day
it answers wrongly, it answers *confidently*.

Measured: ``tests/runtime/test_import_edges.py`` (11 tests over the extractor and the plan),
``tests/providers/test_the_index_answers_who_imports.py`` (5 over a real socket: an edge is
found, a re-sent file replaces it, a deleted file takes it away, a near-miss target is not a
match, a repository outside the credential is never asked).

## P23 — a second implementation, over a service we did not write (2026-10-22)

`tools/serve_index.py` speaks our wire. That makes it a good conformance fixture and a poor
rehearsal: an enterprise already runs a code-search service and will not adopt our JSON. P23
is the seam's second implementation — `providers/retrieve/enterprise.py` — and its value is
not the translation, it is the four rules it keeps *at the boundary* even though the other
side has never heard of them.

1. **Permission is re-checked here.** Repositories the caller named but may not read are
   refused before a request is sent; results the upstream volunteers for other repositories
   are dropped **and counted** (`dropped_results`). An upstream ACL and this deployment's
   constraints are two authorities, and the narrower one wins.
2. **Every answer carries a revision.** A result with no `revision`, no `file`, or a
   `repository` that does not read back as host/namespace/name is **refused**, not dropped:
   dropping turns a broken answer into a shorter one, which reads as "there is nothing
   there". A hit without a revision cannot be re-read — the caller would edit whatever that
   path holds today and call it the version it saw.
3. **"Cannot answer" is never "nothing there".** 5xx, a timeout, a non-JSON body, a failed
   shard — all `IndexUnavailable`. An empty `results` list is the only thing that means
   empty. A service with no `/_links` says it does not answer import questions rather than
   answering none.
4. **Freshness is visible.** The catalog names the revision per repository; repositories it
   omits are *stale* to `lag()`, not fresh — and a catalog with no revisions at all is the
   strongest stale signal, not a reason to assume agreement.

What it is not: a vendor integration. The wire is one plausible foreign shape
(`/_search`, `/_symbols`, `/_slice`, `/_catalog`, `/_links`), named once at module level so a
deployment can subclass it. Measured with `httpx.MockTransport` rather than a socket — the
wire is foreign and the four rules are the subject, which is the opposite of the reference
service, where a real socket found four defects the shape could not.

15 tests in `tests/providers/test_the_enterprise_index_adapter.py`: revision carried through,
a result without one refused (and the message says why), a repository outside the credential
refused *before* the request, an over-returning upstream dropped and counted, a failed shard
refused, an empty list returned as empty, 503 and a non-JSON body as unavailable, symbols with
span and revision, a slice that names its revision, a slice without text refused, a catalog
that makes staleness visible, an omitted revision stale-not-fresh, links answered when present
and named as absent when not.

## P24 — the edge gets a consumer: `forgepilot --dependents` (2026-10-22)

P22 shipped an edge model, a service endpoint and an adapter, and the honest reading of it was
the defect this project has recorded three times: **an implementation nothing could reach**.
`IndexService.importers` was exercised by tests and called by no one. P24 is the consumer, and
it is a CLI command rather than a tool inside the agent loop, for a reason worth stating:

- **A reviewer's question, not a model's.** "Who depends on this module" is asked *before* a
  change is made or while a pull request is read. The tool surface the model sees is already
  six tools plus MCP; adding a seventh for a question the model rarely needs would make the
  loop pay for a capability with no measured use.
- **It pairs with the ingestion command.** `--index-update` is what a deployment runs after a
  push; `--dependents` is what it runs to answer a question about the corpus it just built.
  Same `--index-url` and `--index-token-env`, same failure vocabulary.

```
forgepilot --dependents astropy.modeling \
  --index-url https://search.internal/ --index-repo git.corp/team/api
# git.corp/team/api:paging/limits.py@main  (imports astropy.modeling)
# 1 file(s) import astropy.modeling
```

Three decisions in the output:

1. **`repo:path@ref` per line.** A path alone is ambiguous the moment there are two
   repositories, and a hit without its revision is the confidently-wrong answer this seam
   exists to forbid.
2. **An empty answer exits 0** and says `no file in … imports …`. "Nobody imports it" is a
   real answer to a real question.
3. **Exit 2 is reserved for the two answers that must not be mistaken for it** — a refused
   credential and an unreachable index. Both are printed as errors on stderr; neither can ever
   be read as "nobody imports it".

Measured: `tests/cli/test_dependents_cli.py`, 6 tests against the reference service on a real
socket — the table and the count line, an empty answer exiting 0, a refused token exiting 2,
an unreachable port exiting 2 **without** printing the empty-answer line, the two usage errors,
and `--dependents` refusing to combine with `--index-update`.

## P27 — was the code that was read the code that changed? (2026-10-25)

Every `read_code` result has carried the revision the index served since M14 (`meta.ref`), the
checkout's own revision since P25 (`meta.workingRef`, `meta.stale`), and every delivery has
written its commit since M18. **Nothing read them together.** The question a reviewer actually
has — *was the code this agent read the code it changed?* — could only be answered by
re-reading a transcript, which is the same as not being answered.

`src/forgepilot/runtime/index_audit.py::audit(events)` is a pure fold, and
`forgepilot --index-audit SESSION_ID` is the reader that runs. Three verdicts:

| verdict | meaning | exit |
|---|---|---|
| `consistent` | every repository answered from exactly one revision, no call reported itself stale, and that revision is the delivered commit | 0 |
| `mixed` | some answer came from another revision, or a call said the index lagged the checkout | 3 |
| `unknown` | nothing to compare: no retrieval answered, or the session delivered nothing | 3 |

Three decisions are worth naming because each is a way to get it wrong:

- **`mixed` is not a failure.** An index that updates mid-session is a normal afternoon. The
  verdict exists so a reviewer is *told*, not so a run is failed.
- **`unknown` is never `0`.** Silence is not agreement — the same rule `--check` follows when
  git cannot place a revision. A chat session, a review, an abandoned task: all `unknown`.
- **A branch name is not a revision statement.** `main` cannot be shown to equal a delivered
  sha without resolving it, so `same_revision` matches full shas and git's own 12-character
  abbreviations and nothing else. A deployment whose index reports refs as branch names gets
  `mixed`, and the fix belongs in the provider, not in the comparison.

`code_search` deliberately contributes no revision: its result records the repositories it
searched and which of them were stale (`meta.staleRepos`), but not a per-hit ref. Counting it
as an answer would invent precision the log does not have. It contributes `searches` and
`stale_searches`, and a stale search alone is enough to make a session `mixed`.

Run against the two real sessions this repository produced, the fold says what the prose said:
the task sessions are `consistent`; the sessions whose answers were served from the stale
corpus while the checkout moved on are `mixed` with the stale call named.

## P26 — a real corpus, and the number a synthetic one could not give (2026-10-25)

Two checkouts, not a generator: this repository (`519` tracked files) and the astropy
`5.0` checkout the SWE-bench spike left behind (`~1 800` tracked files). The bench mode is
`tools/bench_index.py --corpus SLUG=PATH` — `git ls-files` for the file set, `symbols_for`
for the symbol table, one shard per repository.

| | |
|---|---|
| corpus | **2 316 file(s), 20 862 symbol(s)**, 31.4 MB of text, 37.5 MB of index |
| build | 15.2 s (parse + symbol extraction, one process) |
| skipped | 4 files over 512 KiB, 75 not UTF-8 (all astropy fixtures) |
| query, exact symbol name | p50 231 ms, p95 **238 ms**, 1 954 hits / 40 queries |
| query, common word (`def`) | p50 35 ms, p95 **36 ms**, 8 000 hits (hit the page cap) |
| query, `def` + `src/**` | p50 33 ms, p95 **34 ms** |

**Recall is measurable now, and the misses are not coverage.** Sampled 200 symbols, asked
each one twice — `/symbols` by name, `/search` by the name as text — and counted whether the
*defining file* came back in the first page:

| sample | `/symbols` definition | `/symbols` rank 1 | `/search` definition | `/search` rank 1 |
|---|---|---|---|---|
| overall (n=212) | 0.906 | 0.623 | 0.849 | 0.481 |
| unambiguous (n=188) | **0.968** | 0.691 | 0.894 | 0.543 |

The bucket matters. A probe over the same sample checked whether every sampled
`(repo, path, name)` was in the index at all: **0 of 150 were missing**. What fails is
*paging*: `__init__` alone has **576** definitions in this corpus, `setup_class` 118,
`evaluate` 94 — ask for one of those and the defining file can be past `max_hits`, with
nothing in the answer saying the page was cut. That is the defect shape one level up (an
answer that is correct and incomplete without saying so), and the fix is a `truncated` flag
on `/symbols` and `/search`, which this milestone does **not** do — it measures the cost
instead, and the two rows above are that cost.

**What the latency numbers are not.** The reference service matches with `IndexCorpus` in
Python, deliberately (`tools/serve_index.py` line 19: a second matcher would make "the same
query" mean two things). So a query is a scan of the corpus text, and the cap explains the
gap between the rows: a common word stops after 200 hits, a rare name reads everything. These
are **not** evidence about an enterprise deployment's p95 — an inverted index would not scan
— and they are not comparable to the P21 synthetic numbers, which were measured on a
generator's small strings. What they do establish is the shape of the cost on real text:
~31 MB of source is ~240 ms per un-narrowed full scan in one process, and a narrowed glob
does not pay for it because the shards that cannot match are never asked.

## P25 — the freshness rule gets a reader (2026-10-25)

`lag()` was written at P11 and had no caller in `src/`: a pure function that could say
"this answer is behind its checkout", reachable from tests and from nothing that runs. Three
readers now exist, and they are deliberately different kinds:

| reader | who sees it | what it says |
|---|---|---|
| `read_code` | the model | one sentence in the tool result: the index answered for `A`, the checkout is at `B` |
| `code_search` | the model | one footer per run, one clause per repository (not per hit) |
| `~--index-update … --check` | an operator or a CI job | exit 0 current, exit 3 behind, exit 3 when git cannot place the indexed revision |

**The third state is the contract.** `retriever.working_ref_of` returns `None` when the
retriever has no `working_ref` at all — a shared enterprise index has no checkout of its own
— and `None` renders as *no sentence*, never as "current". Same rule as the seam's
"cannot answer is not 'nothing there'": an absent note must mean "nobody could tell".

Two defects were found by wiring it, both of the shape this project keeps meeting:

1. **A method that exists and is never called is not a mechanism** — the P25 premise. The
   one exposed defect inside it: `working_ref` must call `_ref` *inside* its `try`, because
   `GrepRetrieverPlugin._git` runs `subprocess.run(cwd=…)` and a configured checkout that is
   not there raises `NotADirectoryError` on Windows, not `UnknownCheckout`.
2. **A machine-wide proxy is not the index's transport.** `httpx` defaults to
   `trust_env=True`, so a stale `ProxyEnable=1 ProxyServer=127.0.0.1:10792` in the Windows
   registry made *every* client call to a locally served index fail with
   `ConnectError: [WinError 10061]` — 20 tests, no code change on either side. Measured with
   one process and one URL: `trust_env=True` → refused, `trust_env=False` → 200. Both index
   clients (`providers/retrieve/remote.py`, `providers/retrieve/enterprise.py`) now pass
   `trust_env=False`: a deployment that needs an egress proxy should configure that proxy
   rather than inherit one silently. The suites also set `NO_PROXY` for loopback
   (`tests/conftest.py`), so a developer's machine settings cannot decide whether the tests
   pass.

**What is still not here.** `lag()` is still not called by a *server*; there is no metric
gauge for it, and the reason is a property rather than an omission: the service does not own
the checkout clock, so a gauge would measure the wrong thing. The reader is the client, which
is where the checkout is.

## P31 — freshness gets a producer (2026-10-25)

P25 gave `lag()` readers on the request path. P31 gives it a *producer that runs when nobody is
asking*, because a freshness rule whose only reader is a tool somebody has to remember to run is
the same "field with no consumer" in slower motion. `tools/index_freshness.py` compares
`refsByRepo` with each checkout's `HEAD` and prints `forgepilot_index_lag_commits`,
`forgepilot_index_age_seconds` and `forgepilot_index_refs`; `deploy/alerts.yml` gained
`ForgePilotIndexStale` and `ForgePilotIndexAge`, and the guard that checks every alert names a
series that exists now scrapes **both** producers — the API and the exporter.

Three decisions worth naming. The series come from an exporter rather than from `/metrics`
because the index cannot see a checkout's `HEAD`; putting the gauge in the API would have meant
shipping the repositories to the service. A missing `updated_at` renders as age 0, not as an
ancient corpus: a service that does not report a timestamp is a gap in the exporter's knowledge,
and an alert on a missing field is an alert people mute. And `head_of` refuses a directory that
is not the root of a checkout — the fourth appearance of the `git -C <dir>` walk-up trap, now
with a test.

What this still does not do: compute lag **without** a local checkout (a server-side index with
no checkout on the same machine has to be told what `HEAD` is by whoever pushed), and prove the
ingest *ran* — the exporter reads `updated_at`, which a run that ingested one repository and
crashed on the second still advances.

## P32 — 60 000 files, and the two things the size exposed (2026-10-25)

`tools/bench_scale.py` generates a reproducible tree (60 000 files across 20 synthetic
repositories, 240 000 symbols, 42 MB of text, a 100.3 MB index), builds it, and measures the
reference service over a real socket. Generate 13.9 s, build 18.8 s (3 191 files/s — synthetic
files are small and parse faster than real code), a 200-file delta ingests at **805 files/s**.
Search:

| shape | in-process p95 | HTTP p95 (20 shards) |
|---|---|---|
| rare token (`needle_`) | 239.9 ms | 461.4 ms |
| common token (`def`) | 108.4 ms | 342.0 ms |
| glob (`pkg03/**`) | 111.0 ms | 4.0 ms |

Three readings, in order of what they change:

1. **`maxHits` was being spent per shard, not per request.** Twenty shards each returning the
   caller's 200 hits meant the service did twenty times the work and truncated afterwards; the
   per-shard share is now `maxHits / shards`, which took the rare-token p95 from 706 ms to
   461 ms with no change to the reply.
2. **The fan-out does not parallelize CPU-bound shards.** `asyncio.gather` over shard searches
   that scan Python objects in-process is still one thread of work, so 20 shards cost about 20
   shard scans. The 342–461 ms figures are the ceiling of *this* reference service, not of the
   seam: a real index answers a token lookup with an inverted index, which is why the contract
   separates "the client" from "the service that answers".
3. **Glob queries are cheap because the path filter runs before the line scan** — 4 ms for
   `pkg03/**` against 60 000 files. That is a property worth keeping in mind when a deployment
   considers turning scoped search into a full-corpus search.

What these numbers are not: recall (a synthetic corpus has no real entropy, so `P26`'s 0.968 is
the number that means something), enterprise p95 (the service is Python scanning objects), or a
multi-replica figure (that is a different process count, not a different corpus).

