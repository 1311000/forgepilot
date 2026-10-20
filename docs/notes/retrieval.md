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

⚠️ Still not here: an actual index service, incremental updates, and a permission model the
service enforces. What Q4 fixes is that the interface, the failure taxonomy and the opt-in
exist and are tested against a mock transport; the service is a deployment's to run.
