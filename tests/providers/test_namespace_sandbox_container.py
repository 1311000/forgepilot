"""The containment assertions, run against a real Linux kernel.

**How to run this on a machine with no Linux host.** Docker Desktop on Windows
*is* a Linux host — a real kernel, real namespaces — so a privileged container
closes the gap that `wsl.exe` could not (it rewrites POSIX-looking arguments and
drops positional parameters, so the provider's argv cannot cross it).

    FORGEPILOT_SANDBOX_CONTAINER=ubuntu:24.04 \
      pytest tests/providers/test_namespace_sandbox_container.py -v

Without that variable the test skips: the ordinary suite should not require
Docker, and it certainly should not pull an image.

**Three requirements of the image**, all measured rather than assumed:

- **`--privileged`.** A default container denies `unshare` outright
  (`unshare(0x20000): Operation not permitted` — no `CAP_SYS_ADMIN`).
- **util-linux `mount`, not busybox's.** Alpine ships busybox, whose `mount`
  cannot remount a bind read-only: `mount -o remount,bind,ro /etc` fails with
  `can't find /etc in /proc/mounts`.
- **An image that already carries util-linux, or a network that can install it.**
  The `apk add -q --no-cache util-linux` below is a no-op on a glibc image and a
  download on Alpine. Measured: with no reachable CDN, `redis:7.4.2-alpine` does
  not fail — it hangs until the 600 s `subprocess` timeout, so the failure reads
  as a sandbox verdict rather than as a missing package. `ubuntu:24.04` has it
  already, needs no network, and passes in 1.6 s, which is why it is the example.

The script that runs is `build_argv`'s own output, byte for byte — only the
trailing `exec "$@"` is replaced, because there is no caller-supplied command to
run; the assertions follow instead.

⚠️ **On Windows the interpreter's integrity label decides whether this runs at
all.** Docker Desktop's engine is reached over a named pipe, and a process at Low
mandatory integrity cannot open it: `docker version` then exits 1 with
`permission denied while trying to connect to the docker API at
npipe:////./pipe/dockerDesktopLinuxEngine`, while `shutil.which("docker")` finds
the binary happily. The workspace venv's `python.exe` carries
`Mandatory Label\\Low Mandatory Level:(I)(NW)` under `icacls`, so every process
started from it is Low — which is why this file was first executed from the base
interpreter with the venv's packages on `PYTHONPATH`, and why the skip reason now
names the failed half instead of assuming the variable was unset. CI is
unaffected: it runs on Linux, where the engine is a Unix socket.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from forgepilot.providers.sandbox.namespace import UserNamespace
from forgepilot.seams.refs import SessionId
from forgepilot.seams.sandbox import ReadScope, SandboxExecutionPolicy, SandboxMode

IMAGE = os.environ.get("FORGEPILOT_SANDBOX_CONTAINER", "")
WORKSPACE = Path("/ws")

CHECKS = """
if echo ok > /ws/inside.txt 2>/dev/null; then w=writable; else w=READ-ONLY; fi
if echo ok > /ws/src/new.py 2>/dev/null; then s=writable; else s=READ-ONLY; fi
if echo x  > /etc/probe 2>/dev/null; then o=WRITABLE; else o=read-only; fi
if echo x  > /ws/.git/hook 2>/dev/null; then g=WRITABLE; else g=read-only; fi
if echo ok > /tmp/scratch 2>/dev/null; then t=writable; else t=READ-ONLY; fi
if cat /ws/keep.txt >/dev/null 2>&1; then r=readable; else r=UNREADABLE; fi
echo "workspace_root=$w subdir=$s outside=$o git=$g tmp=$t existing=$r"
"""

# `read_scope: workspace` — the same boundary, plus the read claim. `/outside`
# is written by the prelude and then masked, so the question is what a command
# inside the boundary can still reach.
CONFINED_CHECKS = """
if echo ok > /ws/inside.txt 2>/dev/null; then w=writable; else w=READ-ONLY; fi
if echo ok > /ws/src/new.py 2>/dev/null; then s=writable; else s=READ-ONLY; fi
if echo x  > /ws/.git/hook 2>/dev/null; then g=WRITABLE; else g=read-only; fi
if cat /outside/secret.txt >/dev/null 2>&1; then o=LEAKED; else o=hidden; fi
if echo x > /outside/poison 2>/dev/null; then p=writable-mask; else p=read-only; fi
if cat /ws/keep.txt >/dev/null 2>&1; then r=readable; else r=UNREADABLE; fi
if echo ok > /tmp/scratch 2>/dev/null; then t=writable; else t=READ-ONLY; fi
if [ -r /etc/passwd ]; then e=etc-visible; else e=ETC-HIDDEN; fi
echo "workspace_root=$w subdir=$s git=$g outside_read=$o outside_write=$p existing=$r tmp=$t toolchain=$e"
"""


def _docker_problem() -> str:
    """Why the containment assertions cannot run here — in the caller's own words.

    The reason used to be one sentence that assumed the only cause was an unset
    variable, so it told a caller who had just set the variable to set it. There
    are three causes and they want three answers.
    """
    if not IMAGE:
        return "FORGEPILOT_SANDBOX_CONTAINER is not set"
    if shutil.which("docker") is None:
        return "docker is not on PATH"
    probe = subprocess.run(["docker", "version"], capture_output=True, check=False)
    if probe.returncode == 0:
        return ""
    lines = probe.stderr.decode("utf-8", "replace").strip().splitlines()
    return f"docker cannot reach the engine (exit {probe.returncode}): " + (
        lines[-1] if lines else probe.stdout.decode("utf-8", "replace").strip()[:120]
    )


_DOCKER_PROBLEM = _docker_problem()


def _docker_usable() -> bool:
    return not _DOCKER_PROBLEM


pytestmark = pytest.mark.skipif(
    bool(_DOCKER_PROBLEM),
    reason=(
        f"{_DOCKER_PROBLEM}. These assertions need a Linux kernel with util-linux "
        "`mount`: point FORGEPILOT_SANDBOX_CONTAINER at an image that carries it "
        "(e.g. ubuntu:24.04). Docker on Windows is a Linux host, and a privileged "
        "container is what reaches it."
    ),
)


def generated_script(
    *, read_scope: ReadScope = ReadScope.HOST, mode: SandboxMode = SandboxMode.WORKSPACE_WRITE
) -> str:
    """The provider's own launcher, with the exec replaced by assertions."""
    from forgepilot.providers.sandbox.namespace import build_argv

    argv = build_argv(
        ["true"],
        policy=SandboxExecutionPolicy(
            mode=mode,
            workspace_root=WORKSPACE,
            session_id=SessionId("container"),
        ),
        cwd=WORKSPACE,
        read_scope=read_scope,
    )
    script = argv[argv.index("-c") + 1]
    assert script.rstrip().endswith('exec "$@"'), script[-120:]
    return script.replace('exec "$@"', "")


@pytest.fixture(scope="module")
def scriptfile(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("sandbox") / "launcher.sh"
    path.write_text(
        "#!/bin/sh\nmkdir -p /ws/.git /ws/src; echo seed > /ws/keep.txt\n"
        + generated_script()
        + CHECKS,
        encoding="ascii",
        newline="\n",  # CRLF would make `2>&1\\r` invalid shell
    )
    return path


@pytest.fixture(scope="module")
def confined_scriptfile(tmp_path_factory) -> Path:
    """The same launcher under a read claim, with a secret outside the workspace.

    The secret is written **before** the masks, which is the only moment it is
    reachable: after them, `/outside` is a fresh tmpfs and the file behind it is
    unreachable rather than merely unwritable — which is the whole difference
    between this and `read_scope: host`.
    """
    path = tmp_path_factory.mktemp("confined") / "launcher.sh"
    path.write_text(
        "#!/bin/sh\nmkdir -p /ws/.git /ws/src /outside/nested\n"
        "echo seed > /ws/keep.txt\n"
        "echo top-secret > /outside/secret.txt\n"
        "echo deeper > /outside/nested/secret.txt\n"
        + generated_script(read_scope=ReadScope.WORKSPACE)
        + CONFINED_CHECKS,
        encoding="ascii",
        newline="\n",
    )
    return path


def _verdict(completed: subprocess.CompletedProcess[str]) -> dict[str, str]:
    line = next(
        (
            candidate
            for candidate in completed.stdout.splitlines()
            if candidate.startswith("workspace_root=")
        ),
        "",
    )
    assert line, (
        f"the launcher produced no verdict.\nstdout: {completed.stdout}\nstderr: {completed.stderr}"
    )
    return dict(part.split("=", 1) for part in line.split())


def run_in_container(script: Path) -> dict[str, str]:
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--privileged",
            "-v",
            f"{script}:/g.sh:ro",
            IMAGE,
            "sh",
            "-c",
            "apk add -q --no-cache util-linux 2>/dev/null; sh /g.sh",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    return _verdict(completed)


def run_argv_as_nobody(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """The real thing: the provider's argv, run by a caller with no privilege.

    Four choices, each load-bearing and each measured rather than assumed:

    - `--user 1000:1000` — the caller N2 is about. A container that runs as root
      would answer a different question and answer it green.
    - `--security-opt seccomp=unconfined` — Docker's default seccomp profile is
      what refuses `clone(CLONE_NEWUSER)`, so leaving it on would make this a
      measurement of the container runtime instead of the kernel
      (`docs/notes/gates-on-linux.md`, N2's caveat).
    - `--tmpfs /ws` — as uid 1000 there is no workspace to seed, and the launcher
      would fail for a reason that has nothing to do with namespaces. `mode=1777`
      is what lets an unprivileged caller write in it.
    - the wrapper seeds `/ws` *outside* the boundary, because `.git` and `src` have
      to exist before the launcher's `only_if_present` rules can protect them —
      the ordering `scriptfile` gets by running its prelude in a privileged
      container. Then `exec "$@"` hands the process over, so what is measured is
      the argv and not a shell around it.

    Deliberately **not** `--privileged`, and not `--cap-add=SYS_ADMIN`: either would
    let the shipped argv succeed as well, erasing the contrast the two tests below
    rest on.
    """
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "1000:1000",
            "--security-opt",
            "seccomp=unconfined",
            "--tmpfs",
            "/ws:mode=1777",
            IMAGE,
            "sh",
            "-c",
            'mkdir -p /ws/.git /ws/src; echo seed > /ws/keep.txt; exec "$@"',
            "sh",
            *argv,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )


def launcher_argv(*, userns: UserNamespace) -> list[str]:
    """`build_argv`'s own output, with the checks as the command *inside* it.

    ⚠️ The **whole** argv, `unshare` included — the one difference from
    `generated_script`, which strips the launcher because a privileged container
    is already the boundary. Here the caller is uid 1000 and that launcher is the
    thing under measurement, so leaving it out would measure the container.
    """
    from forgepilot.providers.sandbox.namespace import build_argv

    return list(
        build_argv(
            ["sh", "-c", CHECKS],
            policy=SandboxExecutionPolicy(
                mode=SandboxMode.WORKSPACE_WRITE,
                workspace_root=WORKSPACE,
                session_id=SessionId("userns"),
            ),
            cwd=WORKSPACE,
            userns=userns,
        )
    )


def test_the_workspace_is_writable_and_everything_else_is_not(scriptfile: Path) -> None:
    """All six properties at once, because they are one boundary.

    Getting the workspace writable *and* the root read-only is the whole trick,
    and the two are in tension: after `/` is remounted read-only, a later bind of
    a subdirectory inherits the flag, so the workspace must be remounted `rw`
    explicitly. That step was missing from the shipped version, which meant every
    write the agent made failed — and it took running this to find out.
    """
    verdict = run_in_container(scriptfile)

    assert verdict["workspace_root"] == "writable", verdict
    assert verdict["subdir"] == "writable", verdict
    assert verdict["outside"] == "read-only", verdict
    assert verdict["git"] == "read-only", verdict
    assert verdict["tmp"] == "writable", verdict
    assert verdict["existing"] == "readable", verdict


def test_a_read_claim_hides_what_it_does_not_name(confined_scriptfile: Path) -> None:
    """Finding 20, closed: the same boundary, with reads confined too.

    A read-only root is *visible* — `cat /eval/evals/tasks/not-in-the-checkout/
    index.json` succeeded on the shipped rules — so the file has to be replaced
    rather than merely protected. The secret is planted outside the workspace,
    the masks go up, and this asserts a command inside can no longer reach it
    while the workspace, the toolchain and `/tmp` are all still usable.
    """
    verdict = run_in_container(confined_scriptfile)

    assert verdict["outside_read"] == "hidden", verdict
    assert verdict["workspace_root"] == "writable", verdict
    assert verdict["subdir"] == "writable", verdict
    assert verdict["existing"] == "readable", verdict
    assert verdict["git"] == "read-only", verdict
    assert verdict["tmp"] == "writable", verdict
    assert verdict["toolchain"] == "etc-visible", verdict
    # ⚠️ `writable-mask` is the intended answer, not a leak. The mask is a fresh
    # tmpfs, so writes land in memory and the host's `/outside` is neither read
    # nor altered — which is why the read above, not this write, is the evidence.
    assert verdict["outside_write"] == "writable-mask", verdict


def _mount_namespace_was_refused(completed: subprocess.CompletedProcess[str]) -> str | None:
    """The measured outcome that is neither containment nor a bug.

    Measured on the `ubuntu-22.04` runner's kernel (the `sandbox` job, uid 1000 in
    an unprivileged `ubuntu:24.04` container): `unshare -U -r --mount` fails before
    it runs a single line of the launcher, with

        unshare: cannot change root filesystem propagation: Permission denied

    That kernel will not let a caller who is only root *inside a new user
    namespace* change the propagation of a mount tree owned by one it is not
    privileged in. ⚠️ **It is not worked around by `--propagation unchanged`**: the
    tree would then stay shared, and the mounts this launcher makes could propagate
    into the host's namespace — the opposite of a boundary. So the refusal is the
    honest answer, and it is the one the provider already gives for that host:
    `probe=none`, `cause=cannot_create_namespace`, with the launcher's own line
    quoted. On Docker Desktop's WSL2 kernel (6.18) the same argv starts and all six
    properties hold; on the 22.04 runner's it does not. Both are measurements.

    ⚠️ This is deliberately **not** a pass by another name. The six assertions
    below still run wherever the launcher starts, and the host-kernel job
    (`sandbox-nonroot`) is where "a non-root caller is contained" is a **gate** —
    there a skip fails the build by design. This module runs `ubuntu:24.04` *inside*
    a 22.04 host, which is not the shape a deployment runs in.
    """
    if (
        completed.returncode == 0
        or "cannot change root filesystem propagation" not in completed.stderr
    ):
        return None
    return completed.stderr.strip()


def test_the_shipped_argv_cannot_contain_a_non_root_caller() -> None:
    """N2 as a negative control: the argv that passes as root fails as uid 1000.

    This is what makes the next test mean something. Without it, a green result
    there could be explained by the container being permissive enough for
    anything, and the period would rest on a measurement with no negative case —
    the same shape as the *other* N1 defect, where a test that measured nothing
    reported itself as a pass.
    """
    completed = run_argv_as_nobody(launcher_argv(userns=UserNamespace.REQUIRE_ROOT))
    combined = completed.stdout + completed.stderr

    assert "workspace_root=" not in combined, (
        "the shipped argv contained a non-root caller, which contradicts N2: " + combined
    )
    assert "Operation not permitted" in combined, combined
    assert completed.returncode != 0, combined


def test_a_non_root_caller_is_contained_by_a_user_namespace() -> None:
    """The mechanism, measured rather than assumed: `-U -r` as uid 1000.

    Identical launcher, identical assertions, one different flag — so a pass here
    is the user namespace and nothing else. This is the row M12 exists for: before
    it the provider's own bundle could not contain a non-root deployment at all,
    and its refusal named the wrong cause.

    Where the kernel refuses the mount-namespace half of the mechanism, this skips
    with the launcher's own words — see `_mount_namespace_was_refused`.
    """
    completed = run_argv_as_nobody(launcher_argv(userns=UserNamespace.MAP_ROOT))
    if refusal := _mount_namespace_was_refused(completed):
        pytest.skip(
            "this kernel will not let a non-root caller make its mount tree private, "
            f"so the user-namespace argv cannot start: {refusal}"
        )
    verdict = _verdict(completed)

    assert verdict["workspace_root"] == "writable", verdict
    assert verdict["subdir"] == "writable", verdict
    assert verdict["outside"] == "read-only", verdict
    assert verdict["git"] == "read-only", verdict
    assert verdict["tmp"] == "writable", verdict
    assert verdict["existing"] == "readable", verdict


def test_the_write_probe_succeeds_for_a_non_root_caller() -> None:
    """The probe's own argv, run the way the probe runs it: exit 0, so `FULL`.

    `_probe_writes` is what decides the verdict `arm` trusts, and it asks by
    running `["true"]` under a read-only policy. Measuring *it* rather than
    inferring it from the boundary test above is the difference between "a
    non-root deployment can be contained" and "a non-root deployment probes
    `full`" — the second is the claim the bundle makes.
    """
    from forgepilot.providers.sandbox.namespace import UserNamespace, build_argv

    argv = build_argv(
        ["true"],
        policy=SandboxExecutionPolicy(
            mode=SandboxMode.READ_ONLY,
            workspace_root=Path("/"),
            session_id=SessionId("userns-probe"),
        ),
        cwd=Path("/"),
        userns=UserNamespace.MAP_ROOT,
    )
    completed = run_argv_as_nobody(argv)

    if refusal := _mount_namespace_was_refused(completed):
        pytest.skip(
            "the probe's own argv cannot start on this kernel either, for the reason "
            f"above — so it would answer `none` here, not `full`: {refusal}"
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr
