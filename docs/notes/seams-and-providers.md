# Seams, providers and the configuration plane

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before adding or replacing a provider, or editing a bundle or profile**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## Provider notes

- **Translation is separated from transport on purpose.** The half that is easy to get wrong (delta accumulation, tool-call indexing, usage extraction) is pure functions with tests; the half that cannot be tested without an endpoint (the HTTP call) is kept thin enough to read in one sitting.
- **Credentials are read from the environment by *name*.** Config carries `api_key_env: FORGEPILOT_LLM_API_KEY`, never the value — a key in a config file ends up in `--dump-config` and in git.
- **A malformed stream chunk raises rather than being skipped.** A chunk we cannot read is a chunk the log cannot reproduce, and skipping would silently shorten history.


## Seam notes (read before adding a provider)

- **Every effectful seam method takes `ExecutionConstraints` and re-checks it.** `Retriever`, `Vcs` and `MemoryStore` do this rather than trusting the caller, and each counts refusals so a test can assert a refusal *happened* instead of inferring it from an absence. A seam where the check is "someone else's job" is where one caller eventually forgets.
- **Revisions are mandatory, not defaulted.** `CodeRef.ref`, `SearchQuery.ref` and `SymbolQuery.ref` have no defaults. A hit that does not name its revision cannot be trusted, and a default of `"HEAD"` would silently mean "trunk" to someone on a branch. This was a real bug during M2: a default caused searches to return nothing.
- **Privilege flags are inherited by omission.** In patch layers, `tier` and `disabled` are three-state: unspecified means "inherit", so an override that only changes `config` cannot silently re-enable a disabled plugin or promote a third-party row to trusted.
- **Mock providers are honest about what they cannot do.** `RecordingSandboxPlugin` records calls, executes nothing, and now reports `executed=False` — it used to answer `exit_code=0`, which a real model read as "the command succeeded and printed nothing" and then reasoned confidently about output that never existed. `LocalTempWorkspacePlugin.diff` returns `""` rather than fabricating a diff; `ReplayLLMPlugin` raises when the script runs out instead of inventing a reply. **A mock that lies is worse than a mock that fails.**
- **Shell tools are refused by default.** `arm` requires `full` sandbox enforcement for `exec`, and the recording sandbox reports `none`, so `run` refuses — and on Windows even a real backend reports at most `partial`. `--allow-uncontained-exec` lowers the requirement for local work and prints a warning when it does. This is a deliberate, visible act, not a default.
- **Failing loudly beats failing confusingly**: reading a directory returns "that is a directory, here is what is in it" rather than an `IsADirectoryError`; an unexecuted command says it did not run rather than reporting `(exit 0)`.


## Configuration plane notes

- **`apply_layers()` is the single composition function** — boot and `--dump-config` both consume its `ResolvedConfig`, which is why they cannot drift. Do not add a second path that reads the config independently.
- An override **replaces a row's whole `config`** rather than deep-merging, so the effective value of a key always has exactly one owning layer.
- Every row carries `sources` (the layers that touched it) and is visible in the dump — that is how "who set the sandbox mode" is answered without reading YAML by hand.
- `${VAR}` / `${VAR:-default}` expand in config values; a missing variable without a default is an error, not an empty string.
- `--dump-config` **never imports anything**, which is deliberate: a profile can be inspected even when a plugin is missing, broken, or not installed on this machine. `--boot` is the command that resolves and loads.

