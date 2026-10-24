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

