"""The Linux namespace sandbox: real containment, and an honest probe.

The previous provider executes with no boundary and says `NONE`. This one
executes *inside* a boundary and can therefore report `FULL` — which is the whole
point, because `arm` refuses `exec` on a host reporting below `full`. With it,
`run` and the project's own checks work in a deployment without
`--allow-uncontained-exec`.

**The logic is a pure function.** Which mounts, in which order, with which
namespace flags is decided by `mount_rules` and `build_argv`, and those are tested
everywhere. Only the syscalls are Linux-only, and those are exercised against a
real kernel — through `unshare` on a Linux runner, or through WSL on this Windows
box when the transport is named explicitly (`FORGEPILOT_SANDBOX_TRANSPORT=wsl`)
— rather than left as a comment.

Ordering is the load-bearing part. Later mounts override earlier ones, so a rule
list is really a precedence order: the workspace is made writable after `/` is
made read-only, and `.git` is made read-only *after* the workspace. **Denials win
by being applied last** — the same property Codex's policy files get from
specificity.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from forgepilot.seams.refs import SessionId
from forgepilot.seams.sandbox import (
    ReadScope,
    SandboxEnforcement,
    SandboxExecutionPolicy,
    SandboxMode,
)
from forgepilot.seams.security import NetworkAccess

WORKSPACE = Path("/srv/checkouts/svc-a")


def policy(
    mode: SandboxMode,
    network: NetworkAccess = NetworkAccess.DENY,
    *,
    workspace_root: Path = WORKSPACE,
):
    return SandboxExecutionPolicy(
        mode=mode,
        workspace_root=workspace_root,
        session_id=SessionId("s-1"),
        network=network,
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

    ⚠️ **Last, in every configuration, and that is a rule rather than an accident
    of ordering.** Rule ordering is precedence; a `/tmp` rule anywhere but the end
    would let a later bind put something back under the tmpfs — which is exactly
    the bug N1 found in the test that was supposed to prove containment, where the
    workspace lived under `/tmp`. Anything the boundary must be able to see has to
    be prepared outside it.
    """
    from forgepilot.providers.sandbox.namespace import MountKind, mount_rules

    for mode in (
        SandboxMode.READ_ONLY,
        SandboxMode.WORKSPACE_WRITE,
        SandboxMode.DANGER_FULL_ACCESS,
    ):
        for read_scope in (ReadScope.HOST, ReadScope.WORKSPACE):
            rules = mount_rules(policy(mode), read_scope=read_scope)
            tmp = next(r for r in rules if r.target == Path("/tmp"))
            assert tmp.kind is MountKind.TMPFS
            assert tmp.read_only is False
            assert rules[-1] is tmp, (
                f"the /tmp tmpfs is not the last rule for {mode.value}/{read_scope.value}: "
                "a rule after it can put a path back under a filesystem that has "
                "just been replaced"
            )


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
        # Every test here drives `probe()`/`run()` through a stand-in launcher, so
        # the mechanism is named explicitly: `auto` tries `-U -r` first, and a
        # python script is not a namespace launcher. The mechanism has its own
        # tests below, where it is the subject rather than the scaffolding.
        {"userns": "require-root", **config},
    )
    try:
        yield root.sandbox
    finally:
        await dispose_fiber(fiber)


def a_launcher(tmp_path: Path, *, exit_code: int, stdout: str = "", stderr: str = "") -> list[str]:
    """A stand-in for `unshare`, so the probe can be observed without patching.

    `enter` is a command line, not a binary name, precisely so this is possible:
    the sandbox flags are appended to it, so a script here receives exactly what
    `unshare` would.

    `stdout` is how the *read* probe is simulated. A real launcher runs the
    command it was handed; this one does not, so whatever that command would have
    printed appears here instead. Empty means "it ran and said nothing", which is
    deliberately **not** the same answer as "it hid the canary".

    `stderr` is how a launcher that *refuses* is simulated — `unshare` explains
    itself there, and the refusal is supposed to quote it rather than paraphrase.
    """
    stamp = hashlib.sha256(f"{stdout}{stderr}".encode()).hexdigest()[:8]
    script = tmp_path / f"launcher-{exit_code}-{stamp}.py"
    log = tmp_path / f"argv-{exit_code}-{stamp}.txt"
    script.write_text(
        "\n".join(
            [
                "import sys, pathlib",
                f"pathlib.Path({str(log)!r}).write_text(chr(10).join(sys.argv[1:]))",
                f"sys.stdout.write({stdout!r})",
                f"sys.stderr.write({stderr!r})",
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
# The read claim: `host` is verified by a write, `workspace` by a read
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("exit_code", "stdout", "expected"),
    [
        (1, "", SandboxEnforcement.NONE),
        (0, "", SandboxEnforcement.PARTIAL),
        (0, "FORGEPILOT-READ-PROBE-OK", SandboxEnforcement.FULL),
        (0, "FORGEPILOT-READ-PROBE-LEAKED\nFORGEPILOT-READ-PROBE-OK", SandboxEnforcement.PARTIAL),
    ],
)
def test_the_read_verdict_has_an_answer_for_every_way_the_probe_can_end(
    exit_code: int, stdout: str, expected: SandboxEnforcement
) -> None:
    """A table, because two of these four rows are the ones that were wrong.

    Row 2 — it ran and printed nothing — must not be `FULL`: no canary came back
    and no marker said the command ran, so nothing was verified, and "unverified"
    is the one answer that must never round up. Row 4 is finding 20: the boundary
    ran the command and handed the canary over. That is a claim *not met*, which
    is `PARTIAL`, not the absence of a boundary.
    """
    from forgepilot.providers.sandbox.namespace import _read_probe_verdict

    assert _read_probe_verdict(exit_code, stdout) is expected


async def test_a_workspace_claim_is_verified_by_a_read_and_not_a_write(tmp_path: Path) -> None:
    """The probe must ask the question the claim makes, not the easy one.

    A write-based probe is what reported `FULL` for a boundary that leaked every
    read (finding 20), so with `read_scope: workspace` the command has to be the
    read of a file outside the workspace — visible in the recorded argv, which is
    the whole reason `enter` is a command line.
    """
    enter, log = a_launcher(tmp_path, exit_code=0, stdout="FORGEPILOT-READ-PROBE-OK")

    async for plugin in _load({"enter": enter, "read_scope": "workspace"}):
        assert plugin.probe() is SandboxEnforcement.FULL

    recorded = log.read_text(encoding="utf-8")
    assert "cat" in recorded and "canary" in recorded, (
        "the workspace claim was verified without reading anything outside it"
    )


async def test_confining_writes_and_not_reads_is_partial_for_workspace_and_full_for_host(
    tmp_path: Path,
) -> None:
    """The same launcher, two claims, two answers — which is the point.

    This is finding 20 in one test: `/` is bound read-only, so a write-based
    probe reports `FULL` while the canary is handed over. `host` claims only what
    that boundary actually does, and keeps its `FULL`; `workspace` claims more,
    and must answer `PARTIAL` rather than borrow the write probe's confidence.
    """
    enter, _ = a_launcher(
        tmp_path,
        exit_code=0,
        stdout="FORGEPILOT-READ-PROBE-LEAKED\nFORGEPILOT-READ-PROBE-OK",
    )

    async for plugin in _load({"enter": enter}):
        assert plugin.probe() is SandboxEnforcement.FULL

    async for plugin in _load({"enter": enter, "read_scope": "workspace"}):
        assert plugin.probe() is SandboxEnforcement.PARTIAL


async def test_run_refuses_when_the_read_claim_could_not_be_verified(tmp_path) -> None:
    """`PARTIAL` is reported rather than flattened to `NONE`, and the message says
    which of the two failures happened — an unavailable sandbox and a boundary
    that keeps half its promise need different fixes, and both are exit 126."""
    enter, _ = a_launcher(
        tmp_path,
        exit_code=0,
        stdout="FORGEPILOT-READ-PROBE-LEAKED\nFORGEPILOT-READ-PROBE-OK",
    )

    async for plugin in _load({"enter": enter, "read_scope": "workspace"}):
        result = await plugin.run(
            ["echo", "hello"],
            policy=policy(SandboxMode.WORKSPACE_WRITE),
            cwd=tmp_path,
            env={},
            timeout_s=5.0,
        )

    assert result.executed is False
    assert result.enforcement is SandboxEnforcement.PARTIAL
    assert "read_scope='workspace'" in result.stderr
    assert "finding 20" in result.stderr
    assert "hello" not in result.stdout


def test_read_scope_does_not_silently_fall_back_to_host() -> None:
    """A typo in the profile must not turn into a weaker claim that reports FULL.

    `read_scope: workspce` on a deployment that meant to confine reads would
    otherwise be accepted, resolve to `host`, and be verified — by the write
    probe — as fully enforced.
    """
    from forgepilot.providers.sandbox.namespace import read_scope_from

    assert read_scope_from("workspace") is ReadScope.WORKSPACE
    assert read_scope_from("host") is ReadScope.HOST

    with pytest.raises(ValueError, match="workspce"):
        read_scope_from("workspce")


# ---------------------------------------------------------------------------
# How the capability is obtained (N2, docs/notes/gates-on-linux.md)
# ---------------------------------------------------------------------------
def test_the_root_is_remounted_rather_than_rebound() -> None:
    """An unprivileged caller may not bind a mount it does not own.

    Measured inside `unshare -U -r`: every mount that came from outside the new
    user namespace is `MNT_LOCKED`, so the launcher's first rule —
    `mount --bind / /` — is refused with `mount: /: wrong fs type, bad option, bad
    superblock on /`, and `set -e` aborts before anything runs. That is why the
    mechanism alone was not enough: with `-U -r` the argv got the capability and
    then died on rule one. A read-only `/` is a flag change, and changing flags is
    permitted where binding is not.
    """
    from forgepilot.providers.sandbox.namespace import MountKind, build_argv, mount_rules

    root = mount_rules(policy(SandboxMode.WORKSPACE_WRITE))[0]

    assert root.target == Path("/")
    assert root.read_only is True
    assert root.kind is MountKind.REMOUNT

    argv = build_argv(["true"], policy=policy(SandboxMode.WORKSPACE_WRITE), cwd=WORKSPACE)
    script = argv[argv.index("-c") + 1]

    assert "mount --bind / /" not in script
    assert "mount -o remount,bind,ro /" in script


def test_map_root_is_the_flags_that_need_no_root() -> None:
    """`-U -r` is where the capability comes from for a deployment that is not root.

    Without it, `--mount --pid --ipc --uts` need `CAP_SYS_ADMIN` in the user
    namespace the caller is *already* in — measured: uid 1000 exits 1 with
    `unshare: unshare failed: Operation not permitted` while `-U -r` exits 0.
    """
    from forgepilot.providers.sandbox.namespace import UserNamespace, build_argv

    argv = build_argv(
        ["true"],
        policy=policy(SandboxMode.WORKSPACE_WRITE),
        cwd=WORKSPACE,
        userns=UserNamespace.MAP_ROOT,
    )

    assert list(argv[:3]) == ["unshare", "-U", "-r"]
    assert "--mount" in argv and "--mount-proc" in argv


def test_require_root_is_the_argv_that_was_always_shipped() -> None:
    """The regression gate: a root deployment keeps exactly the argv it had.

    A new parameter that quietly changed the default would move every deployment
    at once, including the privileged-container path this project measures
    containment against.
    """
    from forgepilot.providers.sandbox.namespace import UserNamespace, build_argv

    default = build_argv(["true"], policy=policy(SandboxMode.WORKSPACE_WRITE), cwd=WORKSPACE)
    explicit = build_argv(
        ["true"],
        policy=policy(SandboxMode.WORKSPACE_WRITE),
        cwd=WORKSPACE,
        userns=UserNamespace.REQUIRE_ROOT,
    )

    assert default == explicit
    assert "-U" not in default and "-r" not in default


def test_auto_is_refused_rather_than_guessed() -> None:
    """`auto` is a question for the machine, not a default for the builder.

    Building an unmeasured argv is how "the probe proved A and the run used B"
    happens. Refusing it here makes that unrepresentable rather than unlikely.
    """
    from forgepilot.providers.sandbox.namespace import UserNamespace, build_argv

    with pytest.raises(ValueError, match="auto"):
        build_argv(
            ["true"],
            policy=policy(SandboxMode.WORKSPACE_WRITE),
            cwd=WORKSPACE,
            userns=UserNamespace.AUTO,
        )


def test_userns_does_not_silently_fall_back() -> None:
    """`userns: maproot` must not resolve to something else.

    Here the wrong value does not weaken a claim, it picks between "works for this
    deployment" and "requires root" — and the deployment would only find out when
    a command was refused.
    """
    from forgepilot.providers.sandbox.namespace import UserNamespace, userns_from

    assert userns_from("auto") is UserNamespace.AUTO
    assert userns_from("map-root") is UserNamespace.MAP_ROOT
    assert userns_from("require-root") is UserNamespace.REQUIRE_ROOT

    with pytest.raises(ValueError, match="maproot"):
        userns_from("maproot")


async def test_auto_records_the_mechanism_the_probe_proved(tmp_path: Path) -> None:
    """The recorded answer is the one `run` builds — not the configured one.

    `build_argv` refuses an unresolved `auto`, so a run can only happen through a
    mechanism `probe()` actually exercised; this asserts the whole way through, on
    the argv the launcher was handed.
    """
    enter, log = a_launcher(tmp_path, exit_code=0)

    async for plugin in _load({"enter": enter, "userns": "auto"}):
        assert plugin.probe() is SandboxEnforcement.FULL
        result = await plugin.run(
            ["echo", "hello"],
            policy=policy(SandboxMode.WORKSPACE_WRITE),
            cwd=tmp_path,
            env={},
            timeout_s=5.0,
        )

    assert result.executed is True
    recorded = log.read_text(encoding="utf-8").splitlines()
    assert recorded[:2] == ["-U", "-r"], "the run used an argv the probe never proved"


def a_launcher_that_refuses_userns(tmp_path: Path) -> tuple[list[str], Path]:
    """Stands in for a kernel that forbids unprivileged user namespaces.

    It fails exactly when the argv asks for one, which is what
    `kernel.apparmor_restrict_unprivileged_userns=1` (or a kernel built without
    `CONFIG_USER_NS`) does to a real `unshare` — so the fallback can be observed
    rather than described.
    """
    script = tmp_path / "launcher-no-userns.py"
    log = tmp_path / "argv-no-userns.txt"
    script.write_text(
        "\n".join(
            [
                "import sys, pathlib",
                f"pathlib.Path({str(log)!r}).write_text(chr(10).join(sys.argv[1:]))",
                'sys.exit(1 if "-U" in sys.argv else 0)',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return [sys.executable, str(script)], log


async def test_auto_falls_back_to_require_root_only_when_already_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback is honest for root and a lie for anyone else.

    As root the fallback runs the shipped argv and verifies it; as an ordinary
    user it would guarantee the same failure again, one process spawn later, with
    a message about the wrong cause — so `auto` does not try it.
    """
    monkeypatch.setattr(
        "forgepilot.providers.sandbox.namespace._effective_uid", lambda: 0, raising=True
    )
    enter, log = a_launcher_that_refuses_userns(tmp_path)

    async for plugin in _load({"enter": enter, "userns": "auto"}):
        assert plugin.probe() is SandboxEnforcement.FULL
        result = await plugin.run(
            ["echo", "hello"],
            policy=policy(SandboxMode.WORKSPACE_WRITE),
            cwd=tmp_path,
            env={},
            timeout_s=5.0,
        )

    assert result.executed is True
    recorded = log.read_text(encoding="utf-8").splitlines()
    assert "-U" not in recorded, "the fallback mechanism is the one without a user namespace"


async def test_a_non_root_host_that_forbids_userns_is_none_not_a_root_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No mechanism is recorded, so nothing runs — the failure is not hidden.

    This is today's non-root deployment: the launcher is present and refuses. The
    point of the test is that the provider does not fall back to an argv that
    needs root, and does not report anything above `none` for a host that cannot
    contain.
    """
    monkeypatch.setattr(
        "forgepilot.providers.sandbox.namespace._effective_uid", lambda: 1000, raising=True
    )
    enter, log = a_launcher_that_refuses_userns(tmp_path)

    async for plugin in _load({"enter": enter, "userns": "auto"}):
        assert plugin.probe() is SandboxEnforcement.NONE
        result = await plugin.run(
            ["echo", "hello"],
            policy=policy(SandboxMode.WORKSPACE_WRITE),
            cwd=tmp_path,
            env={},
            timeout_s=5.0,
        )

    assert result.executed is False
    assert result.enforcement is SandboxEnforcement.NONE
    recorded = log.read_text(encoding="utf-8").splitlines()
    assert recorded[:2] == ["-U", "-r"], "the one attempt made was the one that could work"


# ---------------------------------------------------------------------------
# The refusal names the cause the probe saw — and only that one
# ---------------------------------------------------------------------------
def _sysctl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str | None) -> None:
    """Point the AppArmor check at a file we control — or at one that is not there.

    ``None`` is the Docker Desktop kernel N2 was measured on, and it is the input
    the old unconditional message got wrong: absent is not ``0``.
    """
    path = tmp_path / "apparmor_restrict_unprivileged_userns"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(
        "forgepilot.providers.sandbox.namespace._APPARMOR_USERNS_SYSCTL", path, raising=True
    )


async def test_a_missing_launcher_is_told_apart_from_a_launcher_that_cannot_isolate() -> None:
    """The first of the two failures with one exit code: nothing to enter with.

    Nothing here is about namespaces, so nothing here may mention them — the fix
    is "install the launcher", and the old message sent the reader to a sysctl.
    """
    from forgepilot.providers.sandbox.namespace import SandboxCause

    async for plugin in _load({"enter": ["definitely-not-a-real-launcher-xyz"]}):
        outcome = plugin.diagnose()

        assert outcome.enforcement is SandboxEnforcement.NONE
        assert outcome.cause is SandboxCause.NO_LAUNCHER
        assert outcome.mechanism is None
        refusal = plugin._refusal(outcome)

    assert "is not on PATH" in refusal
    assert "apparmor" not in refusal.lower()


async def test_a_launcher_that_ran_and_failed_quotes_it_and_names_what_was_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider's own paraphrase was the defect; the evidence replaces it.

    `unshare: unshare failed: Operation not permitted` is the line N2 measured, so
    this is what the message must carry — not this codebase's description of it,
    which is the part that can drift from the machine.
    """
    from forgepilot.providers.sandbox.namespace import SandboxCause

    _sysctl(monkeypatch, tmp_path, None)
    enter, _ = a_launcher(
        tmp_path, exit_code=1, stderr="unshare: unshare failed: Operation not permitted"
    )

    async for plugin in _load({"enter": enter, "userns": "auto"}):
        outcome = plugin.diagnose()
        refusal = plugin._refusal(outcome)

    assert outcome.cause is SandboxCause.CANNOT_CREATE_NAMESPACE
    assert "the launcher said: unshare: unshare failed: Operation not permitted" in refusal
    assert "tried: map-root" in refusal
    assert "apparmor" not in refusal.lower()


@pytest.mark.parametrize(
    ("contents", "cause_name", "names_the_sysctl"),
    [
        ("1\n", "APPARMOR_RESTRICTS_USERNS", True),
        ("0\n", "CANNOT_CREATE_NAMESPACE", False),
        (None, "CANNOT_CREATE_NAMESPACE", False),
    ],
)
async def test_three_inputs_give_three_messages_and_only_one_names_apparmor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str | None,
    cause_name: str,
    names_the_sysctl: bool,
) -> None:
    """The pin this phase exists for: no cause is claimed without evidence.

    `1` / `0` / absent are three different machines, and the message has to say
    which one it is looking at. The `0` and absent rows are the ones that used to
    be wrong: both printed the AppArmor explanation, and the absent one is exactly
    the Docker Desktop kernel N2 was measured on.
    """
    from forgepilot.providers.sandbox.namespace import SandboxCause

    _sysctl(monkeypatch, tmp_path, contents)
    enter, _ = a_launcher(tmp_path, exit_code=1, stderr="unshare: unshare failed")

    async for plugin in _load({"enter": enter, "userns": "auto"}):
        outcome = plugin.diagnose()
        refusal = plugin._refusal(outcome)

    assert outcome.cause is SandboxCause[cause_name]
    assert ("kernel.apparmor_restrict_unprivileged_userns" in refusal) is names_the_sysctl
    assert "Refusing to run uncontained" in refusal


async def test_the_two_shapes_of_an_unverified_read_claim_read_differently(
    tmp_path: Path,
) -> None:
    """`PARTIAL` covers "it leaked" and "it said nothing", and they are not the same sentence.

    The old text asserted the leak in both cases, which is the same defect one
    level down: a message that explains a cause the probe did not observe.
    """
    from forgepilot.providers.sandbox.namespace import (
        _READ_PROBE_LEAK,
        _READ_PROBE_MARKER,
        SandboxCause,
    )

    refusals: dict[str, str] = {}
    for label, stdout in (
        ("silent", ""),
        ("leaked", f"{_READ_PROBE_LEAK}\n{_READ_PROBE_MARKER}\n"),
    ):
        enter, _ = a_launcher(tmp_path, exit_code=0, stdout=stdout)
        async for plugin in _load({"enter": enter, "read_scope": "workspace"}):
            outcome = plugin.diagnose()
            assert outcome.enforcement is SandboxEnforcement.PARTIAL
            assert outcome.cause is SandboxCause.READ_CLAIM_UNVERIFIED
            refusals[label] = plugin._refusal(outcome)

    assert "produced no evidence either way" in refusals["silent"]
    assert "reached a file outside its workspace" not in refusals["silent"]
    assert "reached a file outside its workspace" in refusals["leaked"]
    assert refusals["silent"] != refusals["leaked"]


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
# It really contains — on a Linux kernel, through a transport the host names
# ---------------------------------------------------------------------------
def wsl_enter() -> list[str]:
    return ["wsl", "-d", "docker-desktop", "--", "unshare"]


@dataclass(frozen=True)
class Transport:
    """How this process reaches a Linux kernel, and how its filesystem is touched.

    The transport is **named** rather than inferred, because the two candidates
    fail in different ways and the failure is what the reason line has to say:

    * ``unshare`` — what ``src/forgepilot/bundles/kernel_sandbox/patch.yaml`` ships
      and what a production Linux deployment has. Its filesystem *is* this
      process's filesystem, so preparing the workspace is an ordinary ``mkdir``.
    * ``wsl`` — this Windows box's only route to a real kernel, kept as a **manual**
      option (``FORGEPILOT_SANDBOX_TRANSPORT=wsl``) and not as the default. Its
      filesystem is a separate VM, so preparing there needs ``wsl.exe``; and
      ``wsl.exe`` still cannot carry the argv this provider builds — measured: it
      rewrites POSIX-looking arguments into Windows paths (``/bin/sh`` arrived at
      the kernel as ``D:/software/Git/usr/bin/sh``) and drops every positional
      parameter after the first operand following ``-c``, so the trailing
      ``sh -c <script> sh <command…>`` cannot arrive. The containment itself *was*
      verified by hand on that kernel (read-only write refused, ``--net`` yielding
      ``0 /proc/net/route``); only its automation is blocked.
    """

    name: str
    enter: list[str]
    workspace: Path

    def prepare(self) -> None:
        """Make the workspace root exist where the launcher will see it.

        ⚠️ **Not under ``/tmp``, in either transport.** ``mount_rules`` replaces
        ``/tmp`` with a fresh tmpfs as its last rule, so a workspace prepared there
        is bound and then hidden, and the launcher's ``cd`` exits 126 — measured,
        and the reason this fixture lives in the home directory instead.
        """
        raise NotImplementedError

    def cleanup(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class NativeTransport(Transport):
    """``unshare`` on this filesystem — the transport a Linux runner uses."""

    def prepare(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


@dataclass(frozen=True)
class WslTransport(Transport):
    """This Windows box reaching the WSL kernel: manual, and documented as such."""

    def prepare(self) -> None:
        subprocess.run(
            [*self.enter[:-1], "--", "mkdir", "-p", self.workspace.as_posix()], check=True
        )

    def cleanup(self) -> None:
        subprocess.run([*self.enter[:-1], "--", "rm", "-rf", self.workspace.as_posix()], check=True)


def transport() -> Transport:
    """The transport this run is testing, chosen by name rather than by platform.

    ``FORGEPILOT_SANDBOX_TRANSPORT`` exists so the same test can be pointed at the
    WSL kernel by hand; the default is the shipped one, and it is the default
    because the earlier version hardcoded WSL, which is why this test had never
    run anywhere. A test whose transport is a constant can only ever skip.
    """
    name = os.environ.get("FORGEPILOT_SANDBOX_TRANSPORT", "unshare").strip() or "unshare"
    if name == "wsl":
        return WslTransport(
            name="wsl", enter=wsl_enter(), workspace=Path("/root/forgepilot-sandbox-workspace")
        )
    if name != "unshare":
        raise AssertionError(
            f"unknown FORGEPILOT_SANDBOX_TRANSPORT {name!r}: expected unshare or wsl"
        )
    return NativeTransport(
        name="unshare",
        enter=["unshare"],
        workspace=Path.home() / "forgepilot-sandbox-workspace",
    )


# No `skipif` on the platform: whether the boundary can be built is a fact the
# probe *measures*, and a test that decides it from `sys.platform` skips on the
# host where the answer is genuinely "no" for a reason nobody then sees. It is
# still skipped off Linux — but by the probe, with the reason line naming the
# transport and the cause.
def test_the_transport_is_named_rather_than_assumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A constant transport is why this test had never run anywhere.

    `skipif(not ON_LINUX)` and a hardcoded `wsl_enter()` meant the containment
    assertion could only ever be skipped on the box that wrote it *and* on the box
    that would have run it. The default is now what `kernel_sandbox` ships.
    """
    monkeypatch.delenv("FORGEPILOT_SANDBOX_TRANSPORT", raising=False)
    shipped = transport()

    assert shipped.name == "unshare"
    assert shipped.enter == ["unshare"], "the bundled launcher, not a developer's"
    assert str(shipped.workspace) != "/tmp" and "/tmp/" not in f"{shipped.workspace}/", (
        "the workspace is under /tmp, which the last mount rule replaces with a tmpfs"
    )

    monkeypatch.setenv("FORGEPILOT_SANDBOX_TRANSPORT", "wsl")
    assert transport().name == "wsl" and transport().enter == wsl_enter()

    monkeypatch.setenv("FORGEPILOT_SANDBOX_TRANSPORT", "nsenter")
    with pytest.raises(AssertionError, match="nsenter"):
        transport()


def test_preparation_and_cleanup_are_a_round_trip(tmp_path: Path) -> None:
    """`prepare` may only be called on a host the probe has already vouched for.

    The first version prepared first, on a host whose transport did not exist, so a
    `PermissionError` on a Windows home directory arrived in place of the skip it
    was supposed to print — the same ordering defect the test itself had, one level
    up in its fixture. This pins that `prepare`/`cleanup` are at least a usable
    round trip where they *are* called.
    """
    native = NativeTransport(name="unshare", enter=["unshare"], workspace=tmp_path / "ws")

    assert not native.workspace.exists()
    native.prepare()
    assert native.workspace.is_dir()
    native.cleanup()
    assert not native.workspace.exists()


async def test_a_write_outside_the_workspace_is_refused() -> None:
    """The boundary, asserted rather than described.

    `/` is remounted read-only, so a write anywhere but the workspace fails. This
    is the assertion the provider exists for, and it runs against a real Linux
    kernel rather than remaining a comment about what one would do.

    ⚠️ **Two things this test used to get wrong were both in its own setup, not in
    the boundary** (N1): the cwd it handed the launcher was `/tmp/forgepilot-sandbox-test`
    while the writable mount is ``policy.workspace_root``, and that path is under
    ``/tmp`` — which the last mount rule replaces with a fresh tmpfs. So the first
    real run would have measured "the workspace I prepared is not there" and
    reported it as a containment result. The workspace root is now the same path
    in all three places and lives outside ``/tmp``.

    ⚠️ **The preparation happens *after* the probe, and here the order is the whole
    difference between a skip and a crash.** It used to run first, and the
    transport exists only on the host where this test is *skipped* — on the host
    that runs it, the preparation call raised `FileNotFoundError` before the guard
    written to handle exactly that case could answer. The test could not pass
    anywhere, and — worse for the job it exists to do — it could not say *why*
    either.
    """
    transport_ = transport()
    workspace = transport_.workspace

    async for plugin in _load({"enter": transport_.enter, "userns": "auto"}):
        outcome = plugin.diagnose()
        if outcome.enforcement is not SandboxEnforcement.FULL:
            mechanism = outcome.mechanism.value if outcome.mechanism else "none"
            pytest.skip(
                f"no namespace boundary through the {transport_.name!r} transport "
                f"on this host: probe={outcome.enforcement.value} "
                f"cause={outcome.cause.value if outcome.cause else 'none'} "
                f"mechanism={mechanism}" + (f"; {outcome.observed}" if outcome.observed else "")
            )

        # Only now — on a host that just proved it can build the boundary. Preparing
        # first is the defect this ordering exists to prevent: it ran a command only
        # the host that *skips* can run, and raised before the guard could answer.
        transport_.prepare()
        try:
            inside = await plugin.run(
                ["sh", "-c", "echo ok > inside.txt && cat inside.txt"],
                policy=policy(SandboxMode.WORKSPACE_WRITE, workspace_root=workspace),
                cwd=workspace,
                env={"PATH": "/usr/bin:/bin"},
                timeout_s=60.0,
            )
            outside = await plugin.run(
                ["sh", "-c", "echo nope > /etc/forgepilot-should-not-exist"],
                policy=policy(SandboxMode.WORKSPACE_WRITE, workspace_root=workspace),
                cwd=workspace,
                env={"PATH": "/usr/bin:/bin"},
                timeout_s=60.0,
            )
        finally:
            transport_.cleanup()

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
        for scope in (ReadScope.HOST, ReadScope.WORKSPACE):
            argv = build_argv(["true"], policy=policy(mode), cwd=WORKSPACE, read_scope=scope)
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


# ---------------------------------------------------------------------------
# read_scope: workspace — the masks that make the read claim true
# ---------------------------------------------------------------------------
def test_a_host_read_claim_generates_no_masks() -> None:
    """`host` says reads are not confined. A mask there would be a boundary
    nobody asked for, and one the probe is not checking."""
    from forgepilot.providers.sandbox.namespace import mount_rules

    for mode in (SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE):
        assert not any(rule.kind.name == "MASK_UNNAMED" for rule in mount_rules(policy(mode))), (
            f"{mode} grew a mask without a read claim"
        )


def test_a_workspace_read_claim_masks_before_it_binds_the_workspace() -> None:
    """Order again: the mask goes over `/`, and the workspace is bound back on
    top of it. Reversed, the mask would hide the workspace it just restored."""
    from forgepilot.providers.sandbox.namespace import MountKind, mount_rules

    rules = mount_rules(policy(SandboxMode.WORKSPACE_WRITE), read_scope=ReadScope.WORKSPACE)

    mask = next(r for r in rules if r.kind is MountKind.MASK_UNNAMED)
    workspace = next(r for r in rules if r.target == WORKSPACE)

    assert mask.target == Path("/")
    assert mask.keep == (WORKSPACE,), "a mask that does not keep the workspace hides it"
    assert workspace.kind is MountKind.BIND_FROM_CWD
    assert rules.index(mask) < rules.index(workspace)


def test_the_mask_cannot_hide_the_workspace_from_its_own_bind() -> None:
    """⚠️ The trap that decides the implementation.

    `mount(8)` canonicalises its source argument, so once an ancestor of the
    workspace is a mask, `mount --bind . <workspace>` resolves `.` to the *masked*
    path and silently binds an empty directory over the workspace. Taking the cwd
    **before** the masks is what leaves `.` naming the real dentry, and the inode
    comparison is the belt for the same failure — because it is silent: the
    workspace comes back empty, which reads as a broken checkout rather than as a
    boundary bug.
    """
    from forgepilot.providers.sandbox.namespace import build_argv

    argv = build_argv(
        ["true"],
        policy=policy(SandboxMode.WORKSPACE_WRITE),
        cwd=WORKSPACE,
        read_scope=ReadScope.WORKSPACE,
    )
    script = argv[argv.index("-c") + 1]

    assert script.index(f"cd {WORKSPACE.as_posix()}") < script.index("for entry in /*")
    assert "mount -c --bind . " in script, "without --no-canonicalize `.` is a path, not a dentry"
    assert "stat -c %d:%i ." in script
    assert script.index("stat -c %d:%i .") < script.index('exec "$@"')


def test_the_mask_leaves_symlinks_alone() -> None:
    """`/bin -> usr/bin` is the usual layout: masking `/bin` would replace
    `/usr/bin`, and the shell running the script would lose `mount` itself."""
    from forgepilot.providers.sandbox.namespace import build_argv

    argv = build_argv(
        ["true"],
        policy=policy(SandboxMode.WORKSPACE_WRITE),
        cwd=WORKSPACE,
        read_scope=ReadScope.WORKSPACE,
    )
    script = argv[argv.index("-c") + 1]

    assert 'if [ -L "$entry" ]; then continue; fi' in script
    assert script.index('if [ -L "$entry" ]') < script.index("mount -t tmpfs none")


def test_the_named_toolchain_stays_visible_under_a_read_claim() -> None:
    """`ReadScope.WORKSPACE` is "the workspace **and a named toolchain set**".

    A boundary that hid `/usr` would stop the interpreter from starting, which
    fails loudly; one that hid `/etc` would take the resolver and the user
    database with it. Both are named, checked, and therefore a promise the
    provider can keep — rather than an accident of what the mask loop skipped.
    """
    from forgepilot.providers.sandbox.namespace import TOOLCHAIN_ROOTS, build_argv

    argv = build_argv(
        ["true"],
        policy=policy(SandboxMode.WORKSPACE_WRITE),
        cwd=WORKSPACE,
        read_scope=ReadScope.WORKSPACE,
    )
    script = argv[argv.index("-c") + 1]
    case_line = next(line for line in script.splitlines() if line.strip().startswith("case "))

    for root in TOOLCHAIN_ROOTS:
        assert root in case_line, f"{root} would be masked away"


def test_a_read_only_claim_still_lets_the_workspace_be_read() -> None:
    """The near-miss this test exists for: in `read-only` mode there is no
    writable workspace to bind, so the mask loop would have hidden the workspace
    along with everything else — a boundary that refuses the read it claims to
    allow. The rebind happens in both modes for that reason."""
    from forgepilot.providers.sandbox.namespace import MountKind, mount_rules

    rules = mount_rules(policy(SandboxMode.READ_ONLY), read_scope=ReadScope.WORKSPACE)

    workspace = next(r for r in rules if r.target == WORKSPACE)
    assert workspace.kind is MountKind.BIND_FROM_CWD
    assert workspace.read_only is True


def test_danger_full_access_is_not_masked() -> None:
    """That mode asks for no boundary at all — `run()` never sends it here, and
    dressing it in masks would make `probe()`'s answer about a different thing."""
    from forgepilot.providers.sandbox.namespace import MountKind, mount_rules

    rules = mount_rules(policy(SandboxMode.DANGER_FULL_ACCESS), read_scope=ReadScope.WORKSPACE)

    assert not any(rule.kind is MountKind.MASK_UNNAMED for rule in rules)
