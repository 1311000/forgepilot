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

**M12 measured what moving it would cost, and kept the pin.** On `ubuntu-24.04`
(`6.17.0-1022-azure`, run 38250632723) a non-root user's boundary is refused outright
while `kernel.apparmor_restrict_unprivileged_userns` sits at its default `1`, and the
same command is contained as soon as that is `0`. So the main jobs stay on the platform
whose **default** state builds the boundary, and a third job — **`sandbox-nonroot`, on
`ubuntu-24.04`** — measures the host that would otherwise be a guess: it requires the
refusal to name the sysctl *before*, then sets the sysctl to `0` and requires **zero
skips** and `mechanism    : map-root` after. "Lower it" is a **deployment prerequisite**,
written where a deployment reads it (`bundles/kernel_sandbox/patch.yaml`), not something
the provider does to its host.

Two things were found while preparing the first container run — a third while
checking what the workflow actually installs, a fourth while auditing what
actually skips, and two more while auditing what a Linux filesystem and a
Linux-only wheel do to a suite that has only ever run on Windows. **All six have now
been answered, five of them by an actual Linux run** — see "The first Linux run" below —
and each row says which half is measured and which is not, because a claim marked *not
yet measured* is worth more than one that quietly reads as verified:

| # | What | Evidence | What settles it |
|---|---|---|---|
| **N1** | ~~The only test that runs a command through the real boundary **cannot pass anywhere.**~~ **Settled by M12 — it runs, and it goes red when the boundary is broken.** What reading had found, kept because each half was a real defect: the test was `skipif(not ON_LINUX)` with a hardcoded `wsl` transport, it prepared its workspace *before* reaching its own probe guard, and the workspace it prepared was under `/tmp` — which the rules replace with a fresh tmpfs **last** (`providers/sandbox/namespace.py:364`), i.e. *after* the writable workspace bind at `:351`, so the mount happened and the path the process was told to `cd` into was gone again. The three changes: (1) the transport is **named rather than assumed** — `transport()` reads `FORGEPILOT_SANDBOX_TRANSPORT` (default `unshare`; `wsl` still selectable, `tests/providers/test_namespace_sandbox.py:787-866`); (2) preparation runs **behind** the probe, so a host that cannot build the boundary prints the reason instead of raising — measured here as `PermissionError: [WinError 5] … 'C:\Users\Administrator\forgepilot-sandbox-workspace'`, the same defect shape as the one being fixed, one level up in its own fixture; (3) the native workspace is `Path.home() / "forgepilot-sandbox-workspace"`, outside the tmpfs, and the rule is written next to the mount list: **nothing the launcher needs may be prepared under `/tmp`.** Containment's executing instrument is still the `--privileged` container test (image `ubuntu:24.04`) | `tests/providers/test_namespace_sandbox.py:787-981` (`transport()`, `test_a_write_outside_the_workspace_is_refused` at `:917`); `tests/providers/test_namespace_sandbox_container.py:88-431`; `src/forgepilot/providers/sandbox/namespace.py:343-364` | **Measured, on three hosts.** Windows: `47 passed, 1 skipped`, and the skip is now the probe's own sentence — `no namespace boundary through the 'unshare' transport on this host: probe=none cause=no_launcher mechanism=none`. As **uid 1000** in `ubuntu:24.04`: **`48 passed, 0 skipped`**, i.e. the two assertions inside `test_a_write_outside_the_workspace_is_refused` (`inside.exit_code == 0`; `outside.exit_code != 0`) executed for the first time anywhere. In CI: the `sandbox` job's container is `3 passed, 2 skipped` (that kernel refuses the user namespace before the launcher runs — see N2) and `sandbox-nonroot` is `48 passed, 0 skipped`, **zero skips enforced in the workflow** ("a skip here is not a pass") |
| **N2** | ~~`build_argv`'s flags are `["--mount","--pid","--ipc","--uts","--fork","--mount-proc"]` (+`--net`) — no `-U`, no `-r`.~~ **Settled by M12 — the capability is *obtained*, not assumed.** `class UserNamespace(StrEnum)` (`providers/sandbox/namespace.py:173`) has `auto` / `map-root` / `require-root`; `build_argv(..., userns: UserNamespace = UserNamespace.REQUIRE_ROOT)` (`:500`) inserts `_USERNS_FLAGS = ("-U", "-r")` (`:211`) at the front for `map-root`, and **refuses `auto`** (`ValueError: userns='auto' cannot be built into an argv`) because an argv must not depend on the machine that built it. `auto` is what the bundle ships: `probe()` walks `_candidates()` (`:768` — `-U -r` first, and `require-root` only when the caller is already root), records the mechanism it actually proved, and `run()` builds from that recorded value alone (`_mechanism()` raises rather than guess) | `src/forgepilot/providers/sandbox/namespace.py:173-214,500-544,721-796`; `src/forgepilot/bundles/kernel_sandbox/patch.yaml:43-60`; `src/forgepilot/cli/main.py:315-409` | **Measured on a real host kernel.** `ubuntu-24.04`, kernel `6.17.0-1022-azure`, as the non-root user `runner2` (run 38250632723): with `kernel.apparmor_restrict_unprivileged_userns=1` — the default — `probe()` is `none` with `cause=apparmor_restricts_userns`, and the refusal **names that sysctl**; with `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` the same command is contained: `--sandbox-check` → `probe: full` / `mechanism    : map-root` / `result: OK`, exit 0, and the file is `48 passed, 0 skipped`. So the old sentence is retired: a non-root deployment no longer fails on every distro — where the user namespace is permitted, `-U -r` is enough. ⚠️ **Two caveats, both measured.** (1) Inside the **22.04 runner's container** the same argv is refused earlier with `unshare: cannot change root filesystem propagation: Permission denied`, so the two non-root container tests **skip with that line** there (`3 passed, 2 skipped`) rather than assert; `--propagation unchanged` was rejected as a fix, because leaving the tree shared lets this launcher's mounts propagate back into the host. (2) `/` is now a `MountKind.REMOUNT` (`mount -o remount,bind,ro /`), because a mount inherited from outside the new user namespace is `MNT_LOCKED` and `mount --bind / /` is refused — while the flag change is permitted. On Docker Desktop's `6.18.40.1-microsoft-standard-WSL2` both forms pass as root, so that refusal is the kernel's, not the flags' |
| **N3** | The workflow installed `-e ".[dev]"`, and `[dev]` carries neither `web` nor `ast` — so on CI the **HTTP/SSE surface would not run either**: `tests/httpapi/conftest.py:28` is a module-level `pytest.importorskip("fastapi")`, and the production platform would have executed *less* than this Windows dev box (fastapi, uvicorn and sse-starlette are installed here). The AST test has the same shape from the other side — it needs `tree-sitter`, whose Wheel exists only on Linux, and `[dev]` does not ask for it, so the AST path would have run **nowhere at all** | `pyproject.toml:29-44` (`dev`), `:52-70` (`web`, `ast`); `tests/httpapi/conftest.py:28`; `tests/review/test_review_mode.py:331-334,356-364` | fixed in `ci.yml` to `-e ".[dev,web,ast]"`; **the first run of that job is the verification** — for the two extras *and* for the AST path, which may well be red, which is a finding rather than a broken pipeline |
| **N4** | The skip inventory was wrong the first time, and the mistake is reusable: `@pytest.mark.skipif` finds the two decorator gates, but a **module** is gated by `pytestmark = pytest.mark.skipif(...)`, which no decorator pattern sees — and the wholesale disappearances are there. The gates in `tests/`: 2 module-level (`test_namespace_sandbox_container.py:61` on `_docker_usable()`; `test_github_vcs_live.py:39` on two env vars), 2 decorators (`test_namespace_sandbox.py:278` Linux; `test_review_mode.py:356` tree-sitter), 1 `importorskip` (`httpapi/conftest.py:28`), 3 per-test `pytest.skip` (`contract/test_import_graph.py:89,116,130`). So the Linux run newly executes the **container suite** (once a job exports `FORGEPILOT_SANDBOX_CONTAINER` **and** `docker` is on `PATH` — the `sandbox` job supplies both, which is why that job is where containment can first be *executed*), the **AST test**, and the **HTTP/SSE suite** (N3). ⚠️ `test_github_vcs_live.py` must **stay** skipped: it pushes a branch and opens a real pull request, so exporting those two variables into any job turns a gate into an outward-facing act | `tests/providers/test_namespace_sandbox_container.py:54-69`; `tests/providers/test_github_vcs_live.py:36-46` | `pytest tests/ -q -rs` on Linux, then compare the skip reasons against this list |
| **N5** | Case sensitivity is **proved absent as a hazard, not sampled** — the one class of bug a Windows dev box cannot see at all. 934 intra-project `forgepilot.*` imports (405 `src/`, 499 `tests/`, 1 `tools/check.py:82`, 23 `evals/`, 6 `examples/`) plus 35 fixture-package, 15 `import harness\|arms\|report` and 3 `tests.*`; **zero relative (`from .`) imports exist anywhere**; no path component under `src/ tests/ tools/ evals/ examples/` carries an uppercase letter (glob of 9638 paths); the regex `forgepilot(\.[a-z0-9_]+)*\.[a-z0-9_]*[A-Z]` matches **0** imports (the ten hits are Sphinx docstring refs); no case-only twins; `pyproject.toml:83 testpaths = ["tests"]` matches the directory; no `pythonpath`, and no `pytest.ini`/`setup.cfg`/`tox.ini`; 43 `forgepilot.` module refs across 9 YAML files all exact-case | the proof itself, with its exclusions named (`.venv/`, `.forgepilot/`, `.git`) | ✅ **Settled by M17 — a gate now instead of a proof that expires.** `tests/contract/test_the_tree_is_case_exact.py` runs the whole audit at every gate run, and re-measured it while doing so: 442 files under the five trees, 300 `.py`, 1183 intra-project references, **0 violations** — and the 4 `from forgepilot.seams import Retriever, Sandbox, Vcs, Workspace` lines (`tests/contract/test_seam_contracts.py:15`) correctly allowed, because they are *symbols* the package re-exports, which is the one false positive a naive strict check produces. It compares **exact strings** against the set of paths on disk and never asks `is_file()`: on this box `is_file()` answers `True` for `forgepilot/runtime/Events.py`, which is precisely why the audit could not be automated here before. The same file also resolves every `plugin:` row in the YAML (56 rows, comments skipped) and pins `testpaths`, the absence of `pythonpath` and the absence of `pytest.ini`/`setup.cfg`/`tox.ini` |
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

**N2 holds, and M12 has run the experiment this paragraph asked for.** The *user
namespace* is what makes Ubuntu 24.04's AppArmor restriction relevant, and one
`ubuntu-24.04` job now answers both halves with two steps: with
`kernel.apparmor_restrict_unprivileged_userns` at its default `1`, a non-root caller is
refused and the refusal **names that sysctl** (`cause: apparmor_restricts_userns`); after
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, the same command is
contained — `probe: full`, `mechanism    : map-root`, exit 0 — and the boundary file
reports **`48 passed, 0 skipped`**. The first of those is why **the 22.04 pin stands**: on
24.04 the boundary exists only after a host decision, so the sysctl is a documented
**deployment prerequisite** and the main jobs stay on the platform whose default state
builds the boundary. The `::notice::` half of that step matters as much as the assertion —
if a future image permits unprivileged user namespaces anyway, the job says so rather than
going red, because a refusal that names a sysctl which is *not* the cause would be the
defect rather than the fix.

⚠️ **A plain `docker run` container is not obviously a Linux host that can answer this.**
Docker's default seccomp profile is *described* as refusing `clone(CLONE_NEWUSER)`, and
an unprivileged container has no `CAP_SYS_ADMIN` — which is why the shipped container
test runs `--privileged`. **Now measured, and the caveat turns out to be right for a
different reason:** with `--security-opt=seccomp=unconfined` and no `CAP_SYS_ADMIN`,
`unshare -U -r …` *does* start and build the boundary on Docker Desktop's kernel, while
the shipped `require-root` argv is refused — but no container can answer the AppArmor half
at all, because the sysctl that decides it lives in the **host's** `/proc/sys`. That is
why the answer came from a `ubuntu-24.04` *runner* rather than from the local container,
and why the container test keeps its conservative form (`docker run --privileged …`, or
`--cap-add=SYS_ADMIN --security-opt=seccomp=unconfined`).

## The first Linux run

The gate ran on Linux for the first time in this session, in a local `ubuntu:24.04`
container — not in CI, which still had no remote at that moment (the workflow has since run
for real: see "The workflow itself, run for the first time" below). The recipe, because two
of its steps are the difference between a measurement and a story:

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

## The `sandbox` job, and the one assertion that holds on every host

The workflow gained a job for the namespace sandbox rather than a note about it, and
the job's second half is the part that needs `--privileged`: a default container
denies `unshare` outright, so a boundary test that runs unprivileged would pass by
testing the wrong thing. The job pins `FORGEPILOT_SANDBOX_CONTAINER: ubuntu:24.04`
and runs `tests/providers/test_namespace_sandbox_container.py` plus
`forgepilot --sandbox-check` **inside** the container, installing there rather than
mounting the checkout's venv: the boundary names a toolchain set, and an interpreter
outside it is masked, so the check's own command would die `command not found` — a
false negative that looks exactly like a broken sandbox.

The first half is the one that holds everywhere, and it is the reason the check is in
CI at all: **the default profile must exit non-zero.** On this Windows box, on a Linux
runner, and in the container, `recording` reports `none` while the profile's bar is
`full`. That is the assertion a host cannot accidentally satisfy.

The recipe is not aspirational — every command in the workflow's container step was
run by hand first, and the note that it *was* measured is in the step. It has since run
**in CI** (see below), which is what the distinction was for: measured by hand first, then
confirmed by the runner (`probe full`, `shell ran, exit code 0`, `result OK`, and the
container suite's `2 passed`).

## The workflow itself, run for the first time

`.github/workflows/ci.yml` had never executed because the repository had no remote. It does
now — but **not the repository this project's own examples push into**: `1311000/test` is the
fixture `examples/stat-fix/github/` and `tests/providers/test_github_vcs_live.py` operate on
(its `main` is the seeded `metrics-stats` tree and it carries the example's two pull requests),
so the source went to a repository of its own, `1311000/forgepilot`, and `main` was pushed
there. Trigger: `push` on `main`. Run:
[38139694497](https://github.com/1311000/forgepilot/actions/runs/38139694497).

**Both jobs green, in 42 s and 43 s**, and the numbers are the interesting part:

| job | measured |
|---|---|
| `check` | `ruff check .` clean; **`1053 passed, 217 skipped in 27.54s`**; TOTAL 7718 statements, **922 missed, 88.05%**; `Required test coverage of 85.0% reached.`; `All gates passed.` |
| `sandbox` | 8/8 steps. `tests/providers/test_namespace_sandbox_container.py` → **`2 passed in 3.90s`**. `--sandbox-check`: default profile → `claim read_scope: not declared`, `probe none`, `required full`, `shell refused, nothing executed`, exit 1 (asserted by the step); privileged `ubuntu:24.04` → `claim read_scope: host`, `probe full`, `shell ran, exit code 0`, `result OK`. `tests/providers/test_namespace_sandbox.py` → **`31 passed, 1 skipped`** |

**Run [38139967155](https://github.com/1311000/forgepilot/actions/runs/38139967155) is the one
that carried this section, and it reproduced every number above** — `1053 passed, 217 skipped
in 33.24s`, 88.05%, `All gates passed.`, the container `2 passed in 2.71s`, the native
`31 passed, 1 skipped` with the same reason line, and the same two `--sandbox-check` verdicts.
Quoting a single green run would leave the ordinary question open — whether it was the tree or
the day — so the second one is recorded beside it.

⚠️ **The two runs skip the same number of tests, and that is not agreement.** This box reports
`1053 passed, 217 skipped` as well — the counts match, the *sets* do not: here the container
and namespace suites skip for want of a Linux kernel, while the `check` job names no
`FORGEPILOT_SANDBOX_CONTAINER` image, so its container suite skips for a different reason and
its AST and HTTP/SSE suites run (N3, N6). What the run does settle is the line count: **922
missed against 942 here** — executing the POSIX-only paths moves coverage *up*, the direction
the bar was chosen for, and equal pass counts say nothing about it.

**N1's prediction is now measured on the runner it was written for.** The native step is named
"a skip here is not a pass", and a skip is exactly what happened — one test,
`test_a_write_outside_the_workspace_is_refused`, with the reason line:

```
SKIPPED [1] tests/providers/test_namespace_sandbox.py:448: no namespace boundary on this host:
the transport 'wsl' is missing, or it cannot create namespaces for this user (see `_probe_now`)
```

The file's docstring said the first Linux run would "confirm the reason line names the
transport rather than the kernel" — it does, word for word. That is not a red build and not a
silenced one: the test was hardcoded to the `wsl` transport, so on the production platform it
could not run, and **the executing instrument was the `--privileged` container test** (the
`2 passed` above), which is what N1 already said it was.

✅ **The paragraph used to end there, with the question open** — "making the native boundary
reachable for an unprivileged runner is a change to `providers/sandbox/namespace.py` … it needs
its own milestone rather than a rider on the commit that switched CI on." **M12 is that
milestone, and it closed the question:** the transport is named rather than hardcoded, the
workspace is prepared behind the probe and outside `/tmp`, and the capability is *obtained* with
a user namespace instead of assumed. The same file now reports **`48 passed, 0 skipped` in this
very job on `ubuntu-22.04`** — as the non-root `runner`, with no sysctl touched — and
`48 passed, 0 skipped` again on `ubuntu-24.04` once its sysctl is lowered. See "M12: the
unprivileged runner, measured" below.

⚠️ **One annotation on both jobs, dated and not fatal:** `actions/checkout@v4` and
`actions/setup-python@v5` target Node.js 20 and are being forced onto Node.js 24. Nothing to
change today; it is written down because a future red build caused by an action's runtime is
exactly the "gate that fails for a reason unrelated to the change" this file argues against.

## M12: the unprivileged runner, measured

"**A skip here is not a pass**" was the honest fallback for as long as building the boundary
needed root. M12 changed *how the capability is obtained* and then asked a real 24.04 host —
and **the first two runs of the new job both went red**, each for a reason worth keeping:

| first failure | what it actually was |
|---|---|
| `sandbox` job, container suite (`2 failed, 3 passed`) | `unshare: cannot change root filesystem propagation: Permission denied`, before the launcher ran a line: that kernel refuses `unshare -U -r --mount` where Docker Desktop's 6.18 accepts it. Kernel-version dependent, so the two non-root container tests now **skip with that measured line** there instead of asserting — and `--propagation unchanged` was **rejected** as the fix rather than left untried, because leaving the propagation tree shared lets this launcher's mounts propagate back into the host |
| `sandbox-nonroot` job, `before` step | `No module named forgepilot.__main__` from a CLI that *is* importable — and the cause was **permissions**: `/home/runner` is `drwxr-x---`, so `runner2` could not traverse into the checkout and the importer **silently skips** a directory it cannot stat. `PYTHONPATH` pointed straight at it and did nothing. Fixed by `sudo chmod o+x` on the two ancestors plus `o+rX` on the checkout — traversable without being listable |

**Run [38250632723](https://github.com/1311000/forgepilot/actions/runs/38250632723) is the
green one** — `check`, `sandbox`, `sandbox-nonroot`, in 51 s / 29 s / 51 s — and the numbers are
again the interesting part:

| job | measured |
|---|---|
| `check` (`ubuntu-22.04`) | `ruff check .` clean; **`1285 passed, 238 skipped in 35.98s`**; TOTAL 9409 statements, **1066 missed, 88.67%**; `All gates passed.` |
| `sandbox` (`ubuntu-22.04`) | container suite **`3 passed, 2 skipped`** (the two non-root tests skipping on the kernel's propagation refusal; ⚠️ **this run did not print it** — the step ran `-v` without `-rs`, so `3 passed, 2 skipped` was a count rather than a finding here, and Phase E added `-rs` to exactly that step. In [run 38251681827](https://github.com/1311000/forgepilot/actions/runs/38251681827) the same two print their reason: `tests/providers/test_namespace_sandbox_container.py:389: this kernel will not let a non-root caller make its mount tree private, so the user-namespace argv cannot start: unshare: cannot change root filesystem propagation: Permission denied`, and at `:427` the same reason for the probe's own argv — which is precisely what the flag was added for); `tests/providers/test_namespace_sandbox.py` → **`48 passed in 0.46s`**; both `--sandbox-check` verdicts as always — the default profile `probe none` / `refused` / exit 1, and the kernel-sandbox profile `claim read_scope: host`, `probe full`, `mechanism    : map-root`, `cause        : none — the claim was verified`, `shell ran, exit code 0` — **as the non-root `runner`, with no sysctl touched**, because the AppArmor restriction does not exist on that kernel |
| `sandbox-nonroot` (`ubuntu-24.04`, kernel `6.17.0-1022-azure`) | **before** (sysctl `1`, as `runner2`): `probe none`, `cause apparmor_restricts_userns`, the refusal naming `kernel.apparmor_restrict_unprivileged_userns`, exit 1. **after** (`sysctl -w …=0`): `tests/providers/test_namespace_sandbox.py` → **`48 passed in 0.73s`, zero skips**; `--sandbox-check` → `probe full`, `mechanism    : map-root`, `cause        : none — the claim was verified`, `required full`, `shell ran, exit code 0`, exit 0 |

So the two runners answer **different halves** of the claim, which is the reason to keep both:
22.04 shows the mechanism works for an unprivileged caller on a kernel with nothing to lower,
and 24.04 shows what a distro that *does* restrict user namespaces does about it — refuse, name
the sysctl that would change its mind, and then work once a human changes it.

**The pin was re-decided with a measurement in hand** (see the top of this file): 24.04 builds
no boundary at its default sysctl, so the sysctl is a **deployment prerequisite** and the main
jobs stay on 22.04. `sandbox-nonroot` is what keeps that from being an assumption — if a future
image changes the default, the job's `::notice::` branch says so instead of the pin quietly
becoming wrong.

## After the workspace documents left the repository

`CLAUDE.md`, `ForgePilot文档.md` and `docs/实现方案.md` are written for whoever works in this
directory, not for whoever clones it. They were removed from the history, added to
`.gitignore:25-27`, and the rewritten `main` was force-pushed — which re-ran the workflow on a
checkout that no longer contains them:
[run 38140873105](https://github.com/1311000/forgepilot/actions/runs/38140873105), green in
53 s (`check`) and 42 s (`sandbox`), and **the first run whose numbers differ from the table
above**:

| job | measured, on a checkout without the workspace documents |
|---|---|
| `check` | `ruff check .` clean; **`1045 passed, 218 skipped in 32.93s`**; TOTAL 7718 statements, **922 missed, 88.05%**; `All gates passed.` |
| `sandbox` | unchanged: container suite `2 passed`, `--sandbox-check` exit 1 for the default profile and 0 in the `--privileged` container, native `31 passed, 1 skipped` |

Eight fewer passes and one more skip are neither a regression nor noise: they are
`tests/contract/test_the_workspace_instructions_fit_the_budget.py`, whose module-level
`pytest.skip(..., allow_module_level=True)` fires when `CLAUDE.md` is absent. **A module-level
skip counts as one skip, not as its eight tests**, so the file's eight assertions leave the pass
column and a single skip takes their place — the delta is exactly the size of that file.
Coverage does not move (922 missed in both), because those assertions are about prose rather
than about statements. The honest reading is that **the front-door budget and the note table are
checked on a developer's machine and nowhere else** — which is what "workspace guard" in that
file's docstring means, and it is a smaller loss than a guard that aborts collection in a clone.

⚠️ **Removing a file from the history is not the same as deleting it from GitHub.** No ref points
at the old commits any more (`git ls-remote --heads origin` returns `main` alone), but GitHub
keeps unreachable objects until its own garbage collection reaches them: at the time of writing,
`gh api repos/1311000/forgepilot/git/trees/565ff1f` and
`contents/CLAUDE.md?ref=565ff1f` both still answer, and the old Actions runs stay listed for the
same reason. Only GitHub Support can force a purge. For documents whose confidentiality is
"written for whoever works in this directory" rather than a secret, waiting is proportionate —
and nothing sensitive was ever in them: `.env.local` and `.env` have never been committed.

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

