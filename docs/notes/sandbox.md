# The sandbox, and what makes a boundary usable

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before running anything for real, or touching `providers/sandbox/*` or the network boundary**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## The kernel sandbox, and what makes a sandbox *usable*

`providers/sandbox/namespace.py` is the deployment-shaped sandbox: commands run
inside Linux namespaces and read-only bind mounts, and `probe()` can honestly say
`FULL` — which is the whole point, because `arm` refuses `exec` below `full`. With
it, `run` and the project's checks work without `--allow-uncontained-exec`.

**`probe()` runs something.** The obvious implementation — "is `unshare` on
PATH?" — reports `FULL` on a host that has the binary and cannot use it, which is
a real configuration: Ubuntu 24.04's default AppArmor policy blocks unprivileged
user namespaces. `arm` trusts this number, so a presence check would hand out
`exec` on the strength of a file existing. This is the same rule the local
provider obeys from the other end (it returns `NONE` unconditionally, and cannot
be configured otherwise).

**The mount list is a precedence order, not a set.** `/` read-only first, then the
workspace writable, then `PROTECTED_SUBPATHS` (`.git`, `.codex`, `.claude`,
`.agents`) read-only *after* it — because a later mount overrides an earlier one,
so "denials go last" is what makes the workspace's write permission not reach
`.git`. A hook written to `.git/hooks/` runs on the next `git commit`, outside the
sandbox, with the user's privileges.

**`as_posix()` for namespace paths, not `str()`.** The mount targets are resolved
*inside the Linux namespace*, where the separator is always `/`. Using the build
host's gives `\tmp` from a Windows checkout — a path that does not exist there,
from a script that also does not run there. The convention belongs to the target.

**⚠️ No `--` before the command.** `sh -c <script> <name> <args...>`: the operand
after the script is `$0` and the rest is `$@`, which is what `exec "$@"` runs.
Adding a `--` for symmetry with `unshare` above it makes `--` into `$1`, and the
launcher tries to execute an option. That is measured, not theorised — the first
real run reported `sh: exec: line 5: : Permission denied`. **The pure test that
existed at the time asserted the `--` was present**, because the test and the
implementation shared the same wrong assumption; the test now asserts the shape
`sh` actually requires, and says why.

### Writes are confined; reads are not — so the boundary *claims*, and the probe checks the claim

`/` bound read-only is **visible, not hidden**. Every file the filesystem will hand
over stays readable inside the boundary: measured on Linux against these very rules,
`cat /eval/evals/tasks/not-in-the-checkout/index.json` **succeeded and printed the
task**, in the same namespace where writing to that directory returned `Read-only
file system` (finding 20, `docs/notes/evals.md`). A write-based `probe()` reports
`FULL` for that boundary — correctly — and says nothing whatever about reads.

So the claim became explicit. `seams/sandbox.py::ReadScope` is chosen in the profile,
and it is **provider configuration, not part of `SandboxExecutionPolicy`**: a call
site knows what it is about to run, but not how a deployment built its boundary.

- `host` (the default, and what this bundle configures): writes are confined, reads
  reach the filesystem. Verified by a **write** the read-only root must refuse.
- `workspace`: reads outside the workspace fail too. Verified by a **read** of a
  canary that must not come back.

The two probes are the point: a write test cannot tell a boundary from a boundary
that hides nothing, so `workspace` against today's rules answers `PARTIAL` — the claim
is not met — and `run` **refuses** rather than borrowing the write probe's confidence.
`PARTIAL` is now reported as `PARTIAL` (it used to be flattened to `NONE`), because
"no sandbox on this host" and "a sandbox that keeps half its promise" need different
fixes and both arrive as exit 126.

Two details decide whether the probe means anything:

- **The canary must not sit under `/tmp`.** Inside the boundary `/tmp` is a fresh
  tmpfs, so a canary there is invisible in *every* scope and the probe would report
  `FULL` for a boundary that hides nothing — the same trap that hid a prepared
  workspace in the eval harness. It goes under `$HOME`, then `/var/tmp`.
- **Unverifiable is not verified.** No writable canary directory, a command that ran
  and printed no marker, or a non-zero exit each answer `PARTIAL`/`NONE`, never
  `FULL`. And a typo in the profile (`read_scope: workspce`) raises at load instead of
  falling back to the weaker claim and being verified as fully enforced.

`workspace` used to be a *claim with a to-do attached*. The masks are now in the
launcher, and the probe is what proves it rather than asserting it:

- **`/` stays bound read-only, and that is deliberate.** Masking does not need a tmpfs
  root, and keeping `/` real is what keeps the existing
  `/.forgepilot-boundary-check` — a *write* that must fail — meaning what it meant.
  Every top-level entry the boundary does not name is then replaced: an empty tmpfs
  over a directory, `/dev/null` over a file.
- **The cwd is taken before the masks, and that is the load-bearing line.**
  `mount(8)` canonicalises its source argument, so once an ancestor of the workspace
  is a mask, `mount --bind . <workspace>` resolves `.` to the *masked* path and
  silently binds an empty directory over the workspace. `mount -c`
  (`--no-canonicalize`) is what keeps `.` a dentry, and a `stat -c %d:%i` comparison
  against the workspace is the belt: it exits 125 rather than handing over an empty
  checkout. Two ways to notice, because the failure is otherwise silent.
- **Symlinks are skipped.** `/bin -> usr/bin` is the standard layout, so masking
  `/bin` would replace `/usr/bin` — and the shell running the script would no longer
  find `mount`.
- **The toolchain set is named rather than accidental:** `/usr /bin /sbin /lib
  /lib64 /lib32 /libx32 /etc /dev /proc`. That is the second half of what `ReadScope`
  promises — reads outside the workspace *and this set* fail — and `/etc` and `/dev`
  are the honest residual: a secret living in `/etc` is still readable inside the
  boundary.
- **A mask is writable.** The tmpfs over a masked directory accepts writes; they land
  in memory and never reach the host. That is why the *read* is the evidence, not the
  write.

Measured against a real kernel — `ubuntu:24.04`, `--privileged`, the provider's own
generated launcher, `tests/providers/test_namespace_sandbox_container.py`: a secret
planted at `/outside/secret.txt` before the masks comes back **unreadable** inside
them, while `/ws` stays writable, `/ws/.git` stays read-only, `/tmp` stays a fresh
tmpfs and `/etc/passwd` stays readable.

### Telling the model the boundary was the reason

`runtime/sandbox_denial.py`, and the `run` tool surfaces it. Codex issue **#41320**
is the case this exists to prevent: the sandbox denial was stripped from the
model-facing result, the model saw only an exit code, and it responded by looking
for *another execution environment* instead of taking the escalation path it was
given. An agent that cannot see the wall keeps walking into it.

Two levels, and they must not blur:

- **Certain** — `ExecResult.executed` is `False`. Nothing ran, and the provider's
  `stderr` says why. A fact, not an inference; the classifier stays out of it —
  which is `not_executed_reason`'s job, and it is a **two**-way split, not one.
  A missing executable is equally "not executed" and has the opposite remedy, so
  the message names the command in that case and the sandbox only in the other.
  It previously named the sandbox in both; see the `shlex` note in the sandbox
  section for what that cost a real model.
- **Likely** — SIGSYS (128+31, which only a syscall filter produces), or
  `read-only file system` / `operation not permitted` / `permission denied` in the
  output. Reported as "possibly the sandbox", with the signal named, because a
  confident wrong answer here sends the model down the escalation path for what is
  really a bug in the code under test. `127` is deliberately **not** a signal: it
  is `command not found` as much as it is a sandbox outcome.

### The model gets a shell; the project's checks do not

`run` launches `shell_argv(command)` — `/bin/sh -c` on POSIX, `cmd.exe /c` on
Windows. `run_checks` still launches `split_command` as an argv list, and the
asymmetry is deliberate:

- A **model's** `command` is a shell line by intent. Models write
  `pytest -q 2>&1 | tail -30`; that was written in this very repository, and an
  argv-only launch hands `2>&1` and `tail` to pytest as file arguments. It
  happened twice in one observed run.
- A **`project.yaml`** check is a line a human wrote and can read. Keeping it
  argv-only keeps it literal, which is what makes a `&&` in it a mistake rather
  than a feature. Handing *config* to `sh -c` would be a different decision, and
  the pipeline has been careful never to make it.

**A shell adds expressiveness, not authority.** The model could already ask for
any argv it liked; `|` buys it a pipeline it could have assembled by hand. The
boundary that matters is the sandbox around the process, and nothing about it is
relaxed here — which is why this needed no change to the seam, the policy or the
guards.

On Windows this is `cmd.exe` and not Git's `sh`, which is present on a developer
box and absent on a bare host. It is also MSYS, so it rewrites arguments that
look like POSIX paths — the same behaviour that made `wsl.exe` unusable as a
transport. Production is Linux, where `/bin/sh` is the answer and neither caveat
applies.

**Escalation is 0 automatic retries**, matching Codex. A refusal is reported; it
is not retried more loosely, and nothing is relaxed automatically. In ForgePilot's
vocabulary the escalation is an **approval** (a one-shot human answer) and the
relaxation is a **policy** decision — which is why Codex's "bypass for similar
commands in future" maps onto `policy`, not onto a remembered approval.

### Running it without a Linux machine: Docker

**Docker Desktop on Windows is a Linux host** — a real kernel, real namespaces.
That closes the gap `wsl.exe` could not (it rewrites POSIX-looking arguments into
Windows paths and drops positional parameters, so no argv ending in
`sh -c <script> sh <cmd>` can cross it).

```bash
FORGEPILOT_SANDBOX_CONTAINER=redis:7.4.2-alpine \
  pytest tests/providers/test_namespace_sandbox_container.py -v
```

Two requirements, both measured:

- **`--privileged`.** A default container denies `unshare` outright:
  `unshare(0x20000): Operation not permitted` — no `CAP_SYS_ADMIN`.
- **util-linux `mount`, not busybox's.** Alpine ships busybox, whose `mount`
  cannot remount a bind read-only — `mount -o remount,bind,ro /etc` fails with
  `can't find /etc in /proc/mounts`. The recipe above installs util-linux first.

The test runs `build_argv`'s own output, byte for byte; only the trailing
`exec "$@"` is replaced, because there is no caller-supplied command.

### What running it actually found — five defects, none of which any test caught

Every one of these was found by executing the thing, and each was invisible to
the pure tests, which asserted the *rule list* rather than what the sequence does:

1. **The workspace came out read-only.** After `/` is remounted read-only, a
   later bind of a subdirectory **inherits the flag**. The workspace must be
   remounted `rw` explicitly. Without it every write the agent makes fails with
   `Read-only file system` — every fixture, every edit, `run_checks` every time.
   This was the shipped behaviour.
2. **No `set -e`.** A mount that failed did not stop anything: the command ran
   anyway, uncontained, while `probe()` had already said `FULL` and `arm` had
   already allowed `exec`. Silent, and unrecoverable downstream.
3. **No positive boundary check.** `set -e` catches a mount that failed; it
   cannot catch one that "succeeded" without taking effect. The script now writes
   to `/` and refuses to run if it succeeds.
4. **Protected subpaths were bound unconditionally.** A repository has `.git`; it
   almost never has `.codex`, `.claude` *and* `.agents`. Binding a path that is
   not there fails, and with `set -e` that aborts the launcher — so the *rule
   meant to protect* made the sandbox refuse to run on nearly every repository.
   Traced: `mount: /ws/.codex: mount point does not exist`.
5. **Non-ASCII in the generated script.** An em dash in a refusal message, on a
   container with no locale. It is now asserted pure-ASCII.

**`tests/providers/test_namespace_sandbox.py::test_a_write_outside_the_workspace_is_refused`
still skips on Windows**, with the four wsl.exe transport failures named in its
reason. `test_namespace_sandbox_container.py` is the one that runs here.

## Sandbox notes (read before running anything for real)

**Until `providers/sandbox/local.py` landed, nothing in this repository executed a
command.** The recording sandbox answers from a script, the scripted one from a
queue, and `arm` refuses `exec` on any host reporting below `full` enforcement —
which both of them report. So every `task/check` was `could_not_run`, and the
harness could edit files but never establish that an edit was right. M8's repair
loop looked like it worked because the tests fed it a scripted exit code.

`LocalProcessSandboxPlugin` closes that. It spawns the command in the workspace,
captures stdout and stderr separately, holds a timeout, and reports the real exit
code. It provides **no containment at all**, and says so:

- `probe()` returns `NONE` **unconditionally, and is deliberately not read from
  config.** A backend cannot report containment it does not apply, and a knob
  letting an operator set `full` here would turn the one honest signal in the
  sandbox stack into a formality — `arm`'s refusal to run `exec` below `full` is
  built on it. Use a backend that reports `full` where one exists.
- Reaching its execution path therefore takes **two visible acts**: the
  `bundles/local_exec` patch, *and* `--allow-uncontained-exec`. Neither is a
  default; `--boot` on the shipped profile still loads the recording sandbox.

Three defects lived in the code that calls the sandbox, all of them invisible for
exactly one reason — **nothing had ever executed**, so nobody had ever looked at
the argv or the environment:

- **The check commands were chained in the wrong language.** `run_checks` joined
  them with `" && "` and handed the result to `exec` *without a shell*, so
  `pytest -q && mypy src` became `["pytest", "-q", "&&", "mypy", "src"]` — the
  operator became a literal argument and the second command never ran as a
  command. Each is now its own process, stopping at the first failure, with the
  failing command's output labelled. Chaining in Python rather than in a shell is
  deliberate: a shell would mean *project configuration* reached `sh -c`, and
  `run_checks` is still the one path that must not do that. (The `run` tool does
  get a shell — see above — for a model-written line rather than a configured
  one. Two callers, two answers, one module.)
- **`env={}` was never a working value, only an unexercised one.** With no `PATH`
  a bare `pytest` does not resolve; on Windows a child Python without
  `SYSTEMROOT` cannot import `_overlapped` and dies before reaching a test. The
  obvious repair — hand over `os.environ` — is worse: the model can ask for a
  command, and the parent environment holds `FORGEPILOT_LLM_API_KEY`,
  `FORGEPILOT_SIGNING_KEY` and the dev token table, so one `env` call would print
  the LLM credential into a `tool/result` that is logged and shown to the model.
  `runtime/sandbox_env.py` is therefore an **allow-list**: a variable nobody has
  thought about is not inherited, rather than being inherited until someone
  remembers to ban it.
- **`shlex.split(posix=True)` eats backslashes.** On Windows
  `C:\…\python.exe -m pytest` arrived as `C:…python.exe`, the binary was not
  found, and the check reported "could not run" — which reads as a configuration
  problem rather than as the harness mangling its own argv. Production is Linux
  and unaffected, but a check that cannot run where you develop is a loop that
  never closes there. `split_command` now splits the way the platform defines a
  command line, so the same command line yields the same argv on both.

  ⚠️ **That fix went into `run_checks` and not into the `run` tool, and the split
  lived in two places for the whole of P1.** Both had the identical
  `shlex.split(…, posix=True)`. A real run then showed it: the model asked to run
  a Windows-path interpreter, the tool shredded the path, nothing was spawned,
  and the model — told *the sandbox records calls without running them* — wrote
  "the sandbox doesn't actually execute" and stopped trying to run anything,
  including the tests that would have told it whether its fix was right. It still
  produced the correct patch, by reading. `split_command` now lives in
  `runtime/commandline.py`, which is where a fact about the platform belongs, and
  there is one of it. The lesson is not "Windows paths are tricky" — it is that
  **a correction made at one call site is a correction that has not been made**,
  and the only thing that found this one was reading what a run actually did.

**A backend must refuse a policy it cannot enforce** (`PolicyNotEnforceable`, in
the seam). `mode` used to be accepted and never read, so a caller asking for
`read-only` got a command free to write anywhere. Nothing asked for it, which is
why it was harmless — and why it would have stayed harmless right up until
someone relied on it. `WORKSPACE_WRITE` is still accepted by the local backend,
because "runs the command, restricts nothing" is at least *not less* than what
the caller asked for, and every result carries `enforcement=NONE` saying so.

## The network boundary is enforced or refused, never asserted (P4-1)

**`network` was the same defect one branch over, and it survived the `mode` fix.**
`SandboxExecutionPolicy.network` defaults to `DENY`, `arm` narrows it when a tool
declares `NETWORK`, `builtin.run` and `repair.run_checks` both thread
`constraints.network` into the policy — and `local.py` never read it. So the
command ran with an unrestricted network reach **while the model's own scope
section said `Network: deny`** (`orchestration/context_assembly.py`). A boundary
that is asserted to the model and not applied is worse than no boundary at all:
the model reasons from it, so it neither attempts the forbidden route nor reports
one, and the run looks compliant. This is the first entry in the defect table
where the false statement was made *to the model*.

The asymmetry with `mode` is the whole argument, and it is not a
special case: `WORKSPACE_WRITE` is accepted because "runs the command, restricts
nothing" is a **superset** of what was asked — honest. A requested `DENY` that is
not applied is the **opposite** of what was asked. The seam states the rule
outright, so the two branches are the same rule applied to different directions.

What it cost, and what it closed:

- **`local.py` refuses `network is not ALLOW`**, before anything is spawned, with
  a message naming both ways out (a backend that reports `full`, or an explicit
  `network: allow` in the project *and* the caller's grant — they are intersected).
- **The prompt is now honest by construction rather than by a second fix.** A
  `DENY` can only be handed to a backend that enforces it; the local backend
  refuses instead of running. So `Network: deny` in the scope section is true
  wherever it appears, and `tests/security/
  test_the_network_boundary_is_enforced_or_refused.py` pins the two to **one
  source** — the value in the section and the value the sandbox is handed are
  compared, so they cannot drift apart again.
- ⚠️ **Every rig that reaches the local backend's execution path had to say
  `network: allow` out loud** — `tests/providers/test_local_sandbox.py`,
  `tests/tools/test_run_command.py`, `tests/task/test_real_check.py`, and both
  `examples/stat-fix*/project.yaml` with their dev principals. That is the point of
  the change rather than its tax: reaching an uncontained backend is now a
  statement a caller has to make, which is exactly what `--allow-uncontained-exec`
  already means one level up. `configs/projects/example/project.yaml` keeps `deny`
  and says why — a real deployment with the namespace sandbox enforces it.

⚠️ **The refusal reaches the model as a readable result, not a crash.** It is
raised inside the `execute` phase, which the pipeline wraps
(`{type(exc).__name__}: {exc}`, `is_error=True`), so the message naming the network
is what the model reads. That is deliberate and it is the same property
`runtime/sandbox_denial.py` exists for: an agent that cannot see the wall keeps
walking into it — and an agent told the wrong wall exists never walks at all.

**The loop is now verified closed, in both directions** (`tests/task/test_real_check.py`).
A whole `run_task` is decided by a real pytest reading the file the scripted model
really wrote: the fix lands and the task is delivered, or it does not and the task
is not. The negative half earned its place — mutating the fix to something the
seeded test does not look for made the repair loop request a *third* turn and run
the script dry, which is the evidence that the verdict drives the loop rather than
merely being recorded beside it.

