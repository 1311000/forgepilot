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
- **A callable attribute on a plugin is not readable through the service proxy.** Cordis hands a service out as a `_TraceableProxy`, and its `__getattr__` ends with `if not tracker.no_shadow and callable(inner_value): return create_shadow_method(...)` (`cordis_port/utils.py:580-708`). An open `sqlite3.Connection` is callable (`Connection.__call__` exists), so caching one on the instance and reading it back — as `self._conn` *or* as a property — yields a function, and the first use fails with `AttributeError: 'function' object has no attribute 'execute'`. The durable memory store opens a connection **per operation, in a local**, and stores none. A `Path` or a tuple reads back fine, which is what makes the failure look arbitrary: the rule is *keep a plugin's attributes data, not connections (or anything else callable)*.
- **A second provider of a seam must not re-implement the seam's policy.** `providers/memory/sqlite.py` and `providers/memory/memory.py` decide the same two questions — may this be believed, and may this caller see it — so those decisions live once, in `seams/memory.py` as pure functions over the value objects, and both providers are storage. The contract suite follows: `tests/contract/conftest.py::memory_store` hands `test_memory_model.py` one provider at a time, so a new store has to pass the *first* store's cases rather than a set it wrote for itself.


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
- **`--boot` judges a row by its fiber state, and `PENDING` is not a failure.** A row counts against the boot only when it is `FAILED`; the reason comes from `kernel.effects.fiber_error()`, which reads the exception the kernel kept on the fiber rather than anything registration raised. `PENDING` means a declared dependency is unavailable — the fail-closed property working — so it is printed, not counted, and it is deliberately **not awaited**: the kernel does not poll `check` (semantics #10), so awaiting it would hang the command. Both halves are pinned in `tests/kernel/test_kernel_contract.py`.
- **A profile that loads is not a profile that works on this host** — that question is `--sandbox-check`, which shares `_apply_rows` with `--boot` and therefore gained the same per-row reason in M15. Do not merge the two: one reports whether the tree resolved, the other whether the host can contain what it needs.
- **`FORGEPILOT_TMP` is a process-wide temp root, not one provider's setting.** `env.pin_temp_root()` (called from `cli/main.py::main`, restored in a `finally`) proves the directory with a real `mkstemp` before pointing `tempfile.tempdir` at it, and raises `TempRootUnusable` — exit 2, naming the variable and the path — when it cannot. The proof is the point: on this development box `%TEMP%`, `C:\Windows\Temp` and `c:\tmp` all *exist* and all refuse `mkstemp`, so "the directory is there" is not the judgement. `LocalTempWorkspacePlugin`'s own refusal (`_resolve_base`, when `tempfile`'s last resort is the current directory) is unchanged and still the reason it refuses to leave a checkout inside whatever tree the caller stood in.

