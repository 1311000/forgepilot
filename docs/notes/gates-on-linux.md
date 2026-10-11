# The gates on Linux, and the six things reading found

> **This file was split out of `CLAUDE.md` deliberately.** The harness injects only the first
> 65536 bytes of the front door, so `CLAUDE.md` keeps the *rules* and this file keeps the
> *evidence*: what was measured, what went wrong, and what it cost. Nothing injects it
> automatically — **read it before changing `ci.yml`, `tools/check.py`, the coverage bar, or anything that only executes on Linux**.
>
> `CLAUDE.md` names this file in its "Where the rest lives" table; that is the link back.

## The runner is pinned, and six things were found by *reading*

`runs-on` is **`ubuntu-22.04`, not `ubuntu-latest`**. The runner image decides the
kernel's namespace policy, and a security claim about the sandbox rests on that policy —
so a `-latest` that moved from 22.04 to 24.04 would change what a green run *means*,
with no edit to this repository. The pin is a decision about which host the claim is
made on, so it is stated rather than inherited.

Two things were found while preparing the first container run — a third while
checking what the workflow actually installs, a fourth while auditing what
actually skips, and two more while auditing what a Linux filesystem and a
Linux-only wheel do to a suite that has only ever run on Windows. **All six have now
been answered, five of them by an actual Linux run** — see "The first Linux run" below —
and each row says which half is measured and which is not, because a claim marked *not
yet measured* is worth more than one that quietly reads as verified:

| # | What | Evidence | What settles it |
|---|---|---|---|
| **N1** | The only test that runs a command through the real boundary **cannot pass anywhere.** It is `skipif(not ON_LINUX)`, and on Linux its transport is `["wsl", "-d", "docker-desktop", "--", "unshare"]` — `wsl` does not exist there, so `probe()` returns `NONE` (`namespace.py:267`). ⚠️ **The skip is not what happens.** The test prepared its workspace *before* reaching that guard: `subprocess.run(["wsl", …], check=True)` at `:311` raised `FileNotFoundError`, so the `-rs` reason the `sandbox` job exists to print was never printed, and the `check` job (the whole suite) would have gone red on a test that measured nothing. Fixed by moving the preparation behind the guard — and **measured on Linux: it skips instead of crashing**. And when it *can* run, two read-not-measured inconsistencies stand in its way: the writable bind is `policy.workspace_root` (`/srv/checkouts/svc-a`, bound unconditionally at `namespace.py:116`) while the test writes in `/tmp/forgepilot-sandbox-test`, and `/tmp` is replaced by a fresh tmpfs **last** (`namespace.py:129`) — which hides anything prepared under it and makes the launcher's `cd` exit 126. Containment's executing instrument is the `--privileged` container test, whose image is now `ubuntu:24.04` (`redis:7.4.2-alpine` hung on `apk add util-linux` — see "The first Linux run"). On Windows the decorator skips it | `tests/providers/test_namespace_sandbox.py:271-279,311-319`; `tests/providers/test_namespace_sandbox_container.py:103-117` | **Measured.** On Linux that file is `16 passed, 1 skipped`, and the reason at `:323` names the transport: `no namespace boundary on this host: the transport 'wsl' is missing, or it cannot create namespaces for this user`. The reorder is the whole difference between that skip and the `FileNotFoundError` that would have killed the job. ⚠️ The `/tmp`-vs-`workspace_root` inconsistency is **not** exercised on the skipping path, so it stays unmeasured; and switching the native transport to `unshare -U -r` is now supported by N2's measurement but is a decision, not a typo fix |
| **N2** | `build_argv`'s flags are `["--mount","--pid","--ipc","--uts","--fork","--mount-proc"]` (+`--net`) — **no `-U`, no `-r`.** Creating a mount/pid/net namespace needs `CAP_SYS_ADMIN` in the *current* user namespace, so the bundled `enter: [unshare]` requires root or a privileged container. A non-root deployment fails on **every** distro, and the refusal message's explanation (Ubuntu 24.04's AppArmor policy) is not the operative cause for that caller | `src/forgepilot/providers/sandbox/namespace.py:212`; the claims it qualifies are `providers/sandbox/namespace.py:252-253` and `bundles/kernel_sandbox/patch.yaml:24-27` | **Measured** in a `--privileged` container on Docker Desktop's kernel (`6.18.40.1-microsoft-standard-WSL2`): as root both forms exit 0; as uid 1000 the shipped argv exits **1** (`unshare: unshare failed: Operation not permitted`) while `unshare -U -r …` exits **0** and `unshare -U true` exits **0**. The refusal message's AppArmor explanation is not the operative cause for this caller — `/proc/sys/kernel/apparmor_restrict_unprivileged_userns` does not exist on that kernel. ⚠️ The AppArmor half needs an Ubuntu-23.10+/24.04 *host* and cannot be measured from inside a container, so the 22.04 pin stands |
| **N3** | The workflow installed `-e ".[dev]"`, and `[dev]` carries neither `web` nor `ast` — so on CI the **HTTP/SSE surface would not run either**: `tests/httpapi/conftest.py:28` is a module-level `pytest.importorskip("fastapi")`, and the production platform would have executed *less* than this Windows dev box (fastapi, uvicorn and sse-starlette are installed here). The AST test has the same shape from the other side — it needs `tree-sitter`, whose Wheel exists only on Linux, and `[dev]` does not ask for it, so the AST path would have run **nowhere at all** | `pyproject.toml:29-44` (`dev`), `:52-70` (`web`, `ast`); `tests/httpapi/conftest.py:28`; `tests/review/test_review_mode.py:331-334,356-364` | fixed in `ci.yml` to `-e ".[dev,web,ast]"`; **the first run of that job is the verification** — for the two extras *and* for the AST path, which may well be red, which is a finding rather than a broken pipeline |
| **N4** | The skip inventory was wrong the first time, and the mistake is reusable: `@pytest.mark.skipif` finds the two decorator gates, but a **module** is gated by `pytestmark = pytest.mark.skipif(...)`, which no decorator pattern sees — and the wholesale disappearances are there. The gates in `tests/`: 2 module-level (`test_namespace_sandbox_container.py:61` on `_docker_usable()`; `test_github_vcs_live.py:39` on two env vars), 2 decorators (`test_namespace_sandbox.py:278` Linux; `test_review_mode.py:356` tree-sitter), 1 `importorskip` (`httpapi/conftest.py:28`), 3 per-test `pytest.skip` (`contract/test_import_graph.py:89,116,130`). So the Linux run newly executes the **container suite** (once a job exports `FORGEPILOT_SANDBOX_CONTAINER` **and** `docker` is on `PATH` — the `sandbox` job supplies both, which is why that job is where containment can first be *executed*), the **AST test**, and the **HTTP/SSE suite** (N3). ⚠️ `test_github_vcs_live.py` must **stay** skipped: it pushes a branch and opens a real pull request, so exporting those two variables into any job turns a gate into an outward-facing act | `tests/providers/test_namespace_sandbox_container.py:54-69`; `tests/providers/test_github_vcs_live.py:36-46` | `pytest tests/ -q -rs` on Linux, then compare the skip reasons against this list |
| **N5** | Case sensitivity is **proved absent as a hazard, not sampled** — the one class of bug a Windows dev box cannot see at all. 934 intra-project `forgepilot.*` imports (405 `src/`, 499 `tests/`, 1 `tools/check.py:82`, 23 `evals/`, 6 `examples/`) plus 35 fixture-package, 15 `import harness\|arms\|report` and 3 `tests.*`; **zero relative (`from .`) imports exist anywhere**; no path component under `src/ tests/ tools/ evals/ examples/` carries an uppercase letter (glob of 9638 paths); the regex `forgepilot(\.[a-z0-9_]+)*\.[a-z0-9_]*[A-Z]` matches **0** imports (the ten hits are Sphinx docstring refs); no case-only twins; `pyproject.toml:83 testpaths = ["tests"]` matches the directory; no `pythonpath`, and no `pytest.ini`/`setup.cfg`/`tox.ini`; 43 `forgepilot.` module refs across 9 YAML files all exact-case | the proof itself, with its exclusions named (`.venv/`, `.forgepilot/`, `.git`) | nothing to run — but it *expires*: a new import path containing a capital, or a rename that leaves a case-only twin, invalidates it. Re-check when either happens |
| **N6** | The **AST review backend executes for the first time** on the runner, and that changes which branch several *unguarded* review assertions take. `blastradius.py:80 ast_available()` is only `import tree_sitter`, `:93-95 analysis_backend()` returns AST or TEXT, and this box cannot import it (no cp314 Windows wheel), so every review test here exercises **TEXT** | `src/forgepilot/runtime/review/blastradius.py:80,86-90,93-95`; `tests/review/test_review_mode.py:331-334,343-353,356-364` | **Settled, and green.** On Linux `tests/review/test_review_mode.py:334` skipped with `tree-sitter is installed, so the text fallback is not in play`, and `tests/review tests/httpapi` → **59 passed, 1 skipped**: the AST backend and the HTTP/SSE surface ran for the first time anywhere and the assertions hold. Had one been red it would have been a finding about the *backend*, and the fix would still have been the assertion, **not** a re-skip |

**The audit also cleared a list, and the list is the point** — re-deriving it costs the same
afternoon it cost once. Every Windows-shaped construct the grep surfaced is *guarded* in the
code: `tests/tools/test_run_command.py:71-83` (the branch is behind `if os.name == "nt":`, so
POSIX asserts only `parts[1:]`), `src/forgepilot/runtime/commandline.py:42-48,74-76` (POSIX
gets `shlex.split(posix=True)` and `("/bin/sh", "-c", cmd)`),
`src/forgepilot/runtime/sandbox_env.py:30-55` (the allow-list carries the POSIX *and* the
Windows names), `src/forgepilot/seams/security.py:252-267` (`normalise_relative` returns
`None` on a leading `/`, so `tests/security/test_sandbox_env.py:104` still holds on Linux),
`tests/evals/test_left_the_workspace.py:28` (`evals/harness.py:825-827` seeds the
interpreter's parent into `allowed`, and `_ABSOLUTE_POSIX` at `evals/harness.py:754`
takes a **single** separator and leans on that membership test, so `dir /s /b` still
never flags it — the two-slash form it replaced could not see a checkout at `/src` at
all, which is the defect the first Linux run found), `src/forgepilot/fs.py:37-41`
(`os.chmod(target, stat.S_IWRITE)` is the Windows read-only object-file path only),
`tests/providers/test_git_worktree.py:154-177`, `tests/providers/test_local_sandbox.py:74`,
`tests/providers/test_grep_retriever.py:273-324`, `tests/tools/test_edit_file.py:259-290`,
`src/forgepilot/providers/retrieve/grep.py:121-125` (`rg.exe` vs `rg`),
`tests/httpapi/conftest.py:189-202` (uvicorn on port 0) — and
`tests/contract/test_session_store_contract.py:314-343` needs no platform branch at all,
because an in-process lock decides before either side of the `msvcrt`/`fcntl` split at
`src/forgepilot/providers/session_store/jsonl.py:546-554` is reached. There is **no**
`signal`, `os.kill`, `setsid`, `creationflags` or `preexec_fn` anywhere in `src/` or `tests/`.

**Two non-test gates can turn that first run red for reasons that are not regressions**, and
both belong in the log's reading: `pyproject.toml:85 filterwarnings = ["error::RuntimeWarning"]`
(with `:84 asyncio_mode = "auto"`) promotes a *runtime warning* to a failure — and a warning is
exactly what a Windows run never emits; and `[tool.coverage.report] fail_under = 85` was chosen
under a suite where 215–216 tests skip, so a Linux run that *executes* them moves the number
in a way nothing local predicts — **and, measured, it moved it up** (87.41% → 87.70%). Neither
gate fired on the first Linux run, which is why they stay listed as things to watch on the
next interpreter or engine change rather than as things that have already happened. The container suite *did* need the network, and that is measured now:
`redis:7.4.2-alpine` hung for the full 600 s `timeout` in `apk add util-linux` and died
as `subprocess.TimeoutExpired` rather than failing, so the job's image is `ubuntu:24.04`,
which ships `util-linux` and passes in 1.6 s. It executed for the first time in the same
session — from the base interpreter rather than the venv's, for the integrity-label
reason in "The first Linux run".

**N2 holds**, and the *user namespace* is what makes Ubuntu 24.04's AppArmor restriction
relevant — so a `ubuntu-24.04` job running
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` is now the *right*
experiment rather than a guess, because the candidate fix (`unshare -U -r …`) is measured
to work where the shipped argv does not. It is still not in `ci.yml`: what the container
measured is the CAP_SYS_ADMIN half, and the AppArmor half needs an Ubuntu *host* kernel.
The 22.04 pin is the honest default until a 24.04 host answers it.

⚠️ **A plain `docker run` container is not obviously a Linux host that can answer this.**
Docker's default seccomp profile is *described* as refusing `clone(CLONE_NEWUSER)`, and
an unprivileged container has no `CAP_SYS_ADMIN` — which is why the shipped container
test runs `--privileged`. Whether that is genuinely required here is part of what the
first container run measures; the verification command therefore is `docker run
--privileged …` (or `--cap-add=SYS_ADMIN --security-opt=seccomp=unconfined`).

## The first Linux run

The gate ran on Linux for the first time in this session, in a local `ubuntu:24.04`
container — not in CI, which still has no remote. The recipe, because two of its steps are
the difference between a measurement and a story:

```bash
docker run --rm -v <tree>:/work:ro ubuntu:24.04 bash -c '
  apt-get install -y -qq python3 python3-venv git build-essential
  cp -a /work /src && rm -rf /src/.venv /src/.forgepilot /src/.tmp
  cd /src && python3 -m venv .venv-linux
  .venv-linux/bin/pip install -e ".[dev,web,ast]"
  .venv-linux/bin/python tools/check.py'
```

⚠️ **The copy is not tidiness.** Running the suite on the bind mount lets NTFS semantics
decide the git tests (read-only object files, the `onexc`→`chmod` path in
`src/forgepilot/fs.py`), and a green run whose git tests never really deleted anything is a
green run about a different platform. `ubuntu:24.04` rather than the CI pin 22.04 because it
carries python3.12 natively. The scripts lived in `.tmp/` — gitignored, a dev-box instrument
rather than a deliverable — so this block is their durable copy.

**Verdict, first run, before the two fixes below:** `INSTALL_EXIT=0`, installing
`ripgrep-bin-15.2.0` and `tree-sitter-0.26.0` (the wheel prediction above, confirmed by the
resolver); ruff clean; then **`1001 passed, 2 failed, 216 skipped in 31.36s`**, coverage
**87.70%** (7544 statements, 928 missed), above the 85 bar. N1, N2, N4 and N6 are as the
table's rows now say.

**Why the containment suite had never executed anywhere, including here.** Its gate is
`IMAGE and shutil.which("docker") and "docker version" == 0`, and the third term was false
*from Python only*: `docker version` in `pwsh` is fine, while every descendant of the venv's
`python.exe` gets `permission denied while trying to connect to the docker API at
npipe:////./pipe/dockerDesktopLinuxEngine`, whatever the stdio shape. Measured cause:
`whoami /groups` in the shell reports `Mandatory Label\High Mandatory Level S-1-16-12288`,
the same call from Python reports `Low Mandatory Level S-1-16-4096`, and
`icacls .venv\Scripts\python.exe` shows the venv launcher itself carries `Mandatory
Label\Low Mandatory Level:(I)(NW)` — so every process it starts is Low, and a Low process
cannot open that named pipe; the base interpreter carries no label. A dev-box fact, not a
project defect (CI reaches a Unix socket), and the same family as the `WinError 5` confusion
earlier in the session; the test file's docstring now names it. With the base interpreter and
the venv's `site-packages` on `PYTHONPATH` the assertion **passed in 1.6 s** — containment's
first execution, run with `--privileged`, which the shipped `unshare` argv needs (that is
N2).

**The two failures were both real, and both about a platform the code had only ever been
asserted about:**

| failure | what it actually was |
|---|---|
| `tests/evals/test_left_the_workspace.py:56` — `a command that moved into the project root was not caught`, for `cd /d "/src" && dir /s /b` | **`_ABSOLUTE_POSIX` demanded two separators**, so a checkout at a top-level path — `/src`, `/app`, `/repo`: the ordinary container shape, and this run's own path — was invisible to the detector whose whole job is catching a run that read the project's own material. `/s` and `/src` are spelled alike and no shape rule separates them; membership of the project does. The pattern now takes one separator and `_ways_out`'s membership test rejects the switch, pinned by `test_a_checkout_at_a_top_level_path_is_still_caught` |
| `tests/security/test_pre_execute_is_pure.py:136` — `TypeError: super(type, obj): obj must be an instance or subtype of type` | The test asserted the *exception type* of setting an undeclared attribute on a `@dataclass(frozen=True, slots=True)`. Measured across interpreters: **3.12.3 and 3.12.15** raise `FrozenInstanceError` for a declared field but `TypeError` from the generated `__setattr__` for an unknown one, while **3.13.16 and 3.14.8** raise `FrozenInstanceError` for both. `pyproject.toml` declares 3.12 as the floor and `ci.yml` pins `"3.12"`, so this was guaranteed on CI and impossible here. The test now accepts either **and** asserts the property (`assert not hasattr(scope, "sandbox")`) rather than the exception type |

Both fixed, and re-verified **on the container's Python 3.12**: those two files →
**19 passed**.

⚠️ **A later run failed for a reason nobody could read, and that is the lesson.** The second
container run reported `INSTALL_EXIT=1` and then `ModuleNotFoundError: No module named
'forgepilot'`; its pip log lived inside a container that had already exited, so the cause was
unrecoverable. The third run keeps a pip cache on the host and prints `tail -25
/tmp/pip.log`, installed cleanly on the same path, and so proved the second failure
environmental (a transient network/PyPI failure) rather than a fact about this tree. **A
setup step that dies must print its reason while it is alive.**

**One of the two unknowns named earlier in this section was answered from the wheel
metadata, before any Linux machine was involved, and the container run then confirmed it.** `ripgrep-bin` — the `[dev]` dependency the tests need because the `grep`
retriever deliberately has no Python fallback — **does ship Linux binaries**: the PyPI
simple index lists `ripgrep_bin-15.2.0-py3-none-manylinux_2_28_x86_64.musllinux_1_2_x86_64.whl`
(and aarch64/riscv64, and macOS), only 15.1.0 and 15.2.0 exist so `ripgrep-bin>=14.0`
resolves to **15.2.0**, and because those wheels are `py3-none-<platform>` rather than
cp-tagged both 3.12 and 3.14 accept them. `manylinux_2_28` needs glibc ≥ 2.28, which
Ubuntu 22.04 (2.35) and 24.04 (2.39) both satisfy. So `pip install -e ".[dev,web,ast]"`
will not fail on that dependency — and the container run installed exactly
`ripgrep-bin-15.2.0` and `tree-sitter-0.26.0`, so the resolver agreed. The `unshare`
question — whether a runner's non-root user can `unshare -Ur` — is **N2**, and is now
measured: not in the shipped form, yes with `-U -r`.

`tools/check.py` was also read in full before the container run, and **needs no change to
work there**: it resolves ruff *beside the running interpreter* first
(`Path(sys.executable).parent / ("ruff.exe" if sys.platform == "win32" else "ruff")`),
then `PATH`; the pytest step is `sys.executable -m pytest tests/ -q --cov`; and
`use_utf8_output()` runs before anything prints. Two Windows-isms in the *model-facing*
prompt were checked while looking and are **not** defects, which is worth recording
because both would have been easy to report as one: `orchestration/context_assembly.py:131-135`
emits the host dynamically and names **both** shells ("`/bin/sh` on Linux and macOS,
`cmd.exe` on Windows"), and `runtime/commandline.py:42-48,74-76` reaches
`cmd.exe`/`COMSPEC` only under `os.name == "nt"` — POSIX gets `shlex.split(posix=True)`
and `/bin/sh -c`.

## What the first run of the linter found

Worth keeping, because it is the argument for the gate rather than a story about one
linter. 243 findings were mechanical (`ruff --fix`). Of the twenty-one that were not,
**four were real defects — three of them latent**, which is why 962 passing tests had
never noticed:

| finding | what it actually was |
|---|---|
| `seams/security.py` **F821** | `canonical_payload() -> dict[str, Any]` used an `Any` that was never imported. `from __future__ import annotations` makes it a *string*, so nothing failed at runtime — but `typing.get_type_hints()` on the constraints object raises, and anything that introspects annotations breaks. In the security seam, the most load-bearing file here |
| `evals/harness.py` **F821** | the same shape: `events: Sequence[Any]` with no `Sequence` imported. Fixing it then surfaced `UP035` — `Sequence` belongs in `collections.abc` |
| `orchestration/preprocess.py` **F841** | `haystack = title + description`, computed and never used, while the completeness rules read **`description` only**. So a ticket whose *title* says「复现步骤：…」while its description does not is `awaiting_info`. The dead line read as though the rules searched both; it is gone and the behaviour is now stated in a comment — and changing it is a routing decision that wants its own argument, not a tidy-up |
| `tests/conftest.py` **F811** | a `constraints_for(workspace, *, repos=…)` **shadowed from the day it was written** by a later `constraints_for(*operations)`. Nothing imported it and the only call site used the other signature, so it was 22 lines no test could reach |

Two more were **not** defects, and saying so is part of the result: the unused `outcome`
in `tests/task/test_task_mode.py` sits in a test that asserts from the **log** rather than
from the returned value — the repository's own principle, so only the binding went; and the
seven `B008` findings are FastAPI's documented `Depends(...)` idiom, declared precisely
through `flake8-bugbear.extend-immutable-calls` rather than waived wholesale.

