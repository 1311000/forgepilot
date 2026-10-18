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

⚠️ **What is still missing, and must not be claimed from the bundle.** (1) There is no
composite provider, so a deployment cannot have both an index *and* the working-tree
overlay at once; `covers_working_tree: false` is the honest capability, and the index
bundle therefore replaces local retrieval rather than adding to it. (2) The eval task
whose answer is **only** in the index is still unwritten — the shape that would finally
price retrieval, since `find-the-rule`'s own note records that `findstr /s` matches
`code_search` on a local checkout. It still needs a task-level patch row (`EvalTask` has
`project`, not `patches`), and a field added before its first consumer is the defect this
repository's table is about.

