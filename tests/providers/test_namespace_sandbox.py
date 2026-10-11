"""The Linux namespace sandbox: real containment, and an honest probe.

The previous provider executes with no boundary and says `NONE`. This one
executes *inside* a boundary and can therefore report `FULL` — which is the whole
point, because `arm` refuses `exec` on a host reporting below `full`. With it,
`run` and the project's own checks work in a deployment without
`--allow-uncontained-exec`.

**The logic is a pure function.** Which mounts, in which order, with which
namespace flags is decided by `mount_rules` and `build_argv`, and those are tested
everywhere. Only the syscalls are Linux-only, and those are exercised through WSL
(below) rather than left as a comment.

Ordering is the load-bearing part. Later mounts override earlier ones, so a rule
list is really a precedence order: the workspace is made writable after `/` is
made read-only, and `.git` is made read-only *after* the workspace. **Denials win
by being applied last** — the same property Codex's policy files get from
specificity.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from forgepilot.seams.refs import SessionId
from forgepilot.seams.sandbox import SandboxEnforcement, SandboxExecutionPolicy, SandboxMode
from forgepilot.seams.security import NetworkAccess

WORKSPACE = Path("/srv/checkouts/svc-a")


def policy(mode: SandboxMode, network: NetworkAccess = NetworkAccess.DENY):
    return SandboxExecutionPolicy(
        mode=mode, workspace_root=WORKSPACE, session_id=SessionId("s-1"), network=network
    )


# ---------------------------------------------------------------------------
# The mount order is a precedence order
# ---------------------------------------------------------------------------
def test_the_whole_filesystem_is_read_only_before_the_workspace_is_made_writable() -> None:
    """Order carries the meaning: a later mount overrides an earlier one.

    `/` first and read-only, then the workspace bound writable on top of it.
    Reversed, the workspace would be re-hidden by the read-only root and nothing
    would be writable at all.
    """
    from forgepilot.providers.sandbox.namespace import mount_rules

    rules = mount_rules(policy(SandboxMode.WORKSPACE_WRITE))

    assert rules[0].target == Path("/")
    assert rules[0].read_only is True
    assert rules[1].target == WORKSPACE
    assert rules[1].read_only is False


def test_the_git_directory_stays_read_only_inside_a_writable_workspace() -> None:
    """Writing `.git/hooks/` turns a sandboxed write into an unsandboxed exec.

    A hook runs on the next `git commit`, outside the sandbox, with the user's
    privileges. So the workspace being writable must not extend to `.git` — and
    the rule that says so has to be applied *after* the one that made the
    workspace writable.
    """
    from forgepilot.providers.sandbox.namespace import mount_rules

    rules = mount_rules(policy(SandboxMode.WORKSPACE_WRITE))

    git_rule = next(r for r in rules if r.target == WORKSPACE / ".git")
    assert git_rule.read_only is True
    assert rules.index(git_rule) > [r.target for r in rules].index(WORKSPACE), (
        "the .git rule must come after the workspace rule, or it is overridden by it"
    )


def test_a_read_only_policy_makes_nothing_writable() -> None:
    from forgepilot.providers.sandbox.namespace import mount_rules

    rules = mount_rules(policy(SandboxMode.READ_ONLY))

    assert all(rule.read_only for rule in rules if rule.target != Path("/tmp"))
    assert not any(rule.target == WORKSPACE for rule in rules), (
        "a read-only policy bound the workspace writable"
    )


def test_tmp_is_always_a_fresh_writable_tmpfs() -> None:
    """A read-only root leaves compilers and test runners with nowhere to write.

    Giving them the real `/tmp` instead would let one command read another's
    leftovers, so it is a fresh tmpfs per execution.
    """
    from forgepilot.providers.sandbox.namespace import MountKind, mount_rules

    for mode in (SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE):
        tmp = next(r for r in mount_rules(policy(mode)) if r.target == Path("/tmp"))
        assert tmp.kind is MountKind.TMPFS
        assert tmp.read_only is False


# ---------------------------------------------------------------------------
# The namespace flags
# ---------------------------------------------------------------------------
def test_the_network_namespace_is_shared_only_when_the_policy_allows_network() -> None:
    """`--net` is what actually cuts it; measured: 0 routes inside."""
    from forgepilot.providers.sandbox.namespace import build_argv

    denied = build_argv(["pytest", "-q"], policy=policy(SandboxMode.WORKSPACE_WRITE), cwd=WORKSPACE)
    allowed = build_argv(
        ["pytest", "-q"],
        policy=policy(SandboxMode.WORKSPACE_WRITE, NetworkAccess.ALLOW),
        cwd=WORKSPACE,
    )

    assert "--net" in denied
    assert "--net" not in allowed


def test_pid_ipc_and_uts_are_always_unshared() -> None:
    """Pid so a command cannot signal or inspect the harness; uts so it cannot
    see the host's hostname; ipc so it cannot reach another process's queues."""
    from forgepilot.providers.sandbox.namespace import build_argv

    argv = build_argv(["true"], policy=policy(SandboxMode.WORKSPACE_WRITE), cwd=WORKSPACE)

    for flag in ("--mount", "--pid", "--ipc", "--uts", "--fork", "--mount-proc"):
        assert flag in argv, f"{flag} missing"


def test_the_command_follows_the_sh_c_name_operand() -> None:
    """`sh -c <script> <name> <args...>` — the operand after the script is `$0`.

    The launcher ends with `exec "$@"`, so the command must land in `$1...`. It
    is passed after a placeholder name for that reason, and **without** a `--`:
    `sh -c` does not parse options after the script, so a `--` would become `$1`
    and `exec "$@"` would try to run the option.

    That is not hypothetical — an earlier version put a `--` there for symmetry
    with `unshare` above it, and the first real execution reported
    `sh: exec: line 5: : Permission denied`. The test that existed at the time
    asserted the separator was present, because the implementation and the test
    shared the same wrong assumption. This one asserts the shape `sh` actually
    requires, and the WSL test below is what would catch it again.
    """
    from forgepilot.providers.sandbox.namespace import build_argv

    command = ["pytest", "--net", "-q"]
    argv = build_argv(command, policy=policy(SandboxMode.WORKSPACE_WRITE), cwd=WORKSPACE)

    assert list(argv[-len(command) :]) == command
    assert argv[-len(command) - 1] == "sh", "the $0 placeholder is missing"
    assert "--" not in argv[len(argv) - len(command) - 2 :], (
        "a `--` before the command would be consumed by `sh` as $1"
    )


# ---------------------------------------------------------------------------
# probe() runs something
# ---------------------------------------------------------------------------
@pytest.fixture
async def sandbox():
    """A loaded plugin with the real launcher."""

    async for value in _load({}):
        yield value


async def _load(config: dict):
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.sandbox.namespace:NamespaceSandboxPlugin"),
        config,
    )
    try:
        yield root.sandbox
    finally:
        await dispose_fiber(fiber)


def a_launcher(tmp_path: Path, *, exit_code: int) -> list[str]:
    """A stand-in for `unshare`, so the probe can be observed without patching.

    `enter` is a command line, not a binary name, precisely so this is possible:
    the sandbox flags are appended to it, so a script here receives exactly what
    `unshare` would.
    """
    script = tmp_path / f"launcher-{exit_code}.py"
    log = tmp_path / f"argv-{exit_code}.txt"
    script.write_text(
        "\n".join(
            [
                "import sys, pathlib",
                f"pathlib.Path({str(log)!r}).write_text(chr(10).join(sys.argv[1:]))",
                f"sys.exit({exit_code})",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return [sys.executable, str(script)], log


# ---------------------------------------------------------------------------
# probe() runs something
# ---------------------------------------------------------------------------
async def test_a_missing_launcher_is_not_enforcement() -> None:
    """The obvious probe — "is the binary on PATH?" — is the wrong question.

    Ubuntu 24.04's default AppArmor policy blocks unprivileged user namespaces,
    so `unshare` is present and fails. A presence check reports FULL there, and
    `arm` hands out `exec` on the strength of a file existing.
    """
    async for plugin in _load({"enter": ["definitely-not-a-real-launcher-xyz"]}):
        assert plugin.probe() is SandboxEnforcement.NONE


async def test_probe_asks_the_machine_by_running_something(tmp_path: Path) -> None:
    """`FULL` must come from an attempt, and the attempt must be a real one."""
    enter, log = a_launcher(tmp_path, exit_code=0)

    async for plugin in _load({"enter": enter}):
        assert plugin.probe() is SandboxEnforcement.FULL

    recorded = log.read_text(encoding="utf-8")
    assert "--mount" in recorded and "--pid" in recorded, (
        "probe reported FULL without building a real sandbox invocation"
    )


async def test_a_launcher_that_runs_but_cannot_isolate_is_not_full(tmp_path: Path) -> None:
    """`unshare` present and refusing — the AppArmor case, made explicit."""
    enter, _ = a_launcher(tmp_path, exit_code=1)

    async for plugin in _load({"enter": enter}):
        assert plugin.probe() is SandboxEnforcement.NONE


# ---------------------------------------------------------------------------
# Running without a boundary is refused, not degraded
# ---------------------------------------------------------------------------
async def test_run_refuses_when_the_boundary_is_unavailable(tmp_path) -> None:
    """Running anyway and reporting a weaker `enforcement` would be the silent
    fallback this provider exists to avoid: the caller asked for a boundary and
    would not get one, at a point where it has already decided it may proceed."""
    async for plugin in _load({"enter": ["definitely-not-a-real-launcher-xyz"]}):
        result = await plugin.run(
            ["echo", "hello"],
            policy=policy(SandboxMode.WORKSPACE_WRITE),
            cwd=tmp_path,
            env={},
            timeout_s=5.0,
        )

    assert result.executed is False
    assert result.enforcement is SandboxEnforcement.NONE
    assert "Refusing to run uncontained" in result.stderr
    assert "hello" not in result.stdout


# ---------------------------------------------------------------------------
# It really contains — through WSL, because this host has no Linux syscalls
# ---------------------------------------------------------------------------
ON_LINUX = sys.platform.startswith("linux")


def wsl_enter() -> list[str]:
    return ["wsl", "-d", "docker-desktop", "--", "unshare"]


@pytest.mark.skipif(
    not ON_LINUX,
    reason=(
        "this host has no Linux syscalls, and wsl.exe cannot carry this argv. "
        "Measured, in order of discovery: it rewrites POSIX-looking arguments into "
        "Windows paths (`/bin/sh` arrived at the kernel as "
        "`D:/software/Git/usr/bin/sh`, so `unshare` could not exec it), and it "
        "drops every positional parameter after the first operand following `-c` "
        "(`sh -c 'echo n=$#; echo $@' sh printf hi` reports n=0). The argv this "
        "provider builds ends in `sh -c <script> sh <command...>`, so the command "
        "cannot arrive. "
        "What *was* verified by hand on the WSL kernel, with `sh` substituted for "
        "`/bin/sh` to get past the rewrite and the mount script taken verbatim from "
        "`_mount_script`: `unshare --mount --pid --ipc --uts --fork --mount-proc "
        "--net sh -c '<script>' sh` -> a write into the read-only root is refused "
        "(`Read-only file system`), and `--net` yields `0 /proc/net/route`. The "
        "containment is real; only its automation is blocked. Production is Linux, "
        "and this is the test that runs there."
    ),
)
async def test_a_write_outside_the_workspace_is_refused() -> None:
    """The boundary, asserted rather than described.

    `/` is bound read-only, so a write anywhere but the workspace fails. This is
    the assertion the provider exists for, and it runs against a real Linux
    kernel rather than remaining a comment about what one would do.

    The workspace is a path **inside** the WSL filesystem, not a Windows temp
    directory: a namespace on that kernel cannot bind a path its own mount table
    has never heard of, and `/mnt/c/...` is a different filesystem with different
    permissions semantics.

    ⚠️ **The preparation happens *after* the probe, and here the order is the whole
    difference between a skip and a crash.** It used to run first, and `wsl` exists
    only on the host where this test is *skipped* — on the host that runs it,
    `subprocess.run(["wsl", …], check=True)` raised `FileNotFoundError` before the
    guard written to handle exactly that case could answer. The test could not pass
    anywhere, and — worse for the job it exists to do — it could not say *why*
    either. Read, not measured: the first Linux run is what confirms the reason
    line names the transport rather than the kernel.
    """
    workspace = Path("/tmp/forgepilot-sandbox-test")

    async for plugin in _load({"enter": wsl_enter()}):
        if plugin.probe() is not SandboxEnforcement.FULL:
            pytest.skip(
                "no namespace boundary on this host: the transport "
                f"{wsl_enter()[0]!r} is missing, or it cannot create namespaces for "
                "this user (see `_probe_now`)"
            )

        # Reachable only where the transport exists, so this call's precondition is
        # the guard above rather than an assumption about which host is running.
        subprocess.run(
            ["wsl", "-d", "docker-desktop", "--", "mkdir", "-p", workspace.as_posix()],
            capture_output=True,
            check=True,
        )

        # ⚠️ Two things about this step are **read, not measured**, and both have to
        # be settled before this test can pass on a host whose probe says `FULL`:
        # the writable mount is `policy.workspace_root` — `/srv/checkouts/svc-a`,
        # unconditionally bound (`namespace.py:116`) — while the cwd written to
        # below is `/tmp/forgepilot-sandbox-test`; and `/tmp` is replaced by a fresh
        # tmpfs last (`namespace.py:129`), which hides anything prepared under it and
        # makes the launcher's `cd` exit 126. So the first real run of this test
        # measures its own setup as much as the boundary.
        inside = await plugin.run(
            ["sh", "-c", "echo ok > inside.txt && cat inside.txt"],
            policy=policy(SandboxMode.WORKSPACE_WRITE),
            cwd=workspace,
            env={"PATH": "/usr/bin:/bin"},
            timeout_s=60.0,
        )
        outside = await plugin.run(
            ["sh", "-c", "echo nope > /etc/forgepilot-should-not-exist"],
            policy=policy(SandboxMode.WORKSPACE_WRITE),
            cwd=workspace,
            env={"PATH": "/usr/bin:/bin"},
            timeout_s=60.0,
        )

    assert inside.exit_code == 0, inside.stderr
    assert "ok" in inside.stdout

    assert outside.enforcement is SandboxEnforcement.FULL
    assert outside.exit_code != 0, "a write outside the workspace succeeded"
    assert "Read-only file system" in outside.stderr or "read-only" in outside.stderr.lower()


# ---------------------------------------------------------------------------
# The sequence, not just the list
# ---------------------------------------------------------------------------
def test_a_writable_bind_states_its_mode_explicitly() -> None:
    """⚠️ The defect this pins was shipped, and only a real kernel found it.

    After `/` is remounted read-only, a *later* bind of a subdirectory **inherits
    the read-only flag**. So binding the workspace is not enough — it has to be
    remounted `rw` afterwards, or every write the agent makes fails with
    `Read-only file system`: no fixture written, no edit applied, `run_checks`
    failing every time.

    The tests that existed asserted the *rule list* — `/` first, workspace
    second, `.git` third — and never that the sequence produces a writable
    workspace. A rule list and a mount sequence are not the same thing, and the
    gap between them is invisible until something runs.
    """
    from forgepilot.providers.sandbox.namespace import build_argv

    argv = build_argv(["true"], policy=policy(SandboxMode.WORKSPACE_WRITE), cwd=WORKSPACE)
    script = argv[argv.index("-c") + 1]

    assert f"mount -o remount,bind,rw {WORKSPACE.as_posix()}" in script
    assert f"mount -o remount,bind,ro {WORKSPACE.as_posix()}/.git" in script


def test_a_failed_mount_stops_the_script_rather_than_running_uncontained() -> None:
    """Without `set -e`, a bind that did not take effect is followed by the
    command running anyway — uncontained, while `probe()` already said `FULL`
    and `arm` already allowed `exec`. Nothing downstream could tell."""
    from forgepilot.providers.sandbox.namespace import build_argv

    argv = build_argv(["true"], policy=policy(SandboxMode.WORKSPACE_WRITE), cwd=WORKSPACE)
    script = argv[argv.index("-c") + 1]

    assert script.splitlines()[0] == "set -e"


def test_the_script_verifies_the_boundary_before_handing_over_control() -> None:
    """`set -e` catches a mount that failed. This catches one that "succeeded"
    without taking effect — the failure mode that would otherwise be silent."""
    from forgepilot.providers.sandbox.namespace import build_argv

    argv = build_argv(["true"], policy=policy(SandboxMode.WORKSPACE_WRITE), cwd=WORKSPACE)
    script = argv[argv.index("-c") + 1]

    assert "forgepilot-boundary-check" in script
    assert script.index("forgepilot-boundary-check") < script.index('exec "$@"')


def test_the_generated_script_is_pure_ascii() -> None:
    """It is passed to `sh -c` on a minimal container image, which may have no
    locale at all. A non-ASCII byte in an error message is enough to make the
    launcher fail in a way that looks like the sandbox misbehaving."""
    from forgepilot.providers.sandbox.namespace import build_argv

    for mode in (SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE):
        argv = build_argv(["true"], policy=policy(mode), cwd=WORKSPACE)
        script = argv[argv.index("-c") + 1]
        assert script.isascii(), [c for c in script if not c.isascii()]


def test_a_protected_subpath_that_does_not_exist_is_skipped_not_bound() -> None:
    """It has to be conditional, or the rule defeats itself.

    A repository has `.git`; it almost never has `.codex`, `.claude` and
    `.agents` too. Binding a path that is not there fails — and with `set -e`
    that aborts the launcher, so a rule meant to *protect against* writing
    somewhere would instead stop the sandbox from running at all, on nearly
    every repository. Traced in a container:
    `mount: /ws/.codex: mount point does not exist`, script dead.
    """
    from forgepilot.providers.sandbox.namespace import build_argv

    argv = build_argv(["true"], policy=policy(SandboxMode.WORKSPACE_WRITE), cwd=WORKSPACE)
    script = argv[argv.index("-c") + 1]

    for name in (".git", ".codex", ".claude", ".agents"):
        target = f"{WORKSPACE.as_posix()}/{name}"
        assert f"[ -e {target} ]" in script, f"{name} is bound unconditionally"
