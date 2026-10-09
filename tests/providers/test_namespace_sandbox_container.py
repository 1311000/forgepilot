"""The containment assertions, run against a real Linux kernel.

**How to run this on a machine with no Linux host.** Docker Desktop on Windows
*is* a Linux host — a real kernel, real namespaces — so a privileged container
closes the gap that `wsl.exe` could not (it rewrites POSIX-looking arguments and
drops positional parameters, so the provider's argv cannot cross it).

    FORGEPILOT_SANDBOX_CONTAINER=redis:7.4.2-alpine \
      pytest tests/providers/test_namespace_sandbox_container.py -v

Without that variable the test skips: the ordinary suite should not require
Docker, and it certainly should not pull an image.

**Two requirements of the image**, both measured rather than assumed:

- **`--privileged`.** A default container denies `unshare` outright
  (`unshare(0x20000): Operation not permitted` — no `CAP_SYS_ADMIN`).
- **util-linux `mount`, not busybox's.** Alpine ships busybox, whose `mount`
  cannot remount a bind read-only: `mount -o remount,bind,ro /etc` fails with
  `can't find /etc in /proc/mounts`. The `redis:7.4.2-alpine` recipe therefore
  installs util-linux first.

The script that runs is `build_argv`'s own output, byte for byte — only the
trailing `exec "$@"` is replaced, because there is no caller-supplied command to
run; the assertions follow instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from forgepilot.seams.refs import SessionId
from forgepilot.seams.sandbox import SandboxExecutionPolicy, SandboxMode

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


def _docker_usable() -> bool:
    if not IMAGE or shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "version"], capture_output=True, check=False)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_usable(),
    reason=(
        "set FORGEPILOT_SANDBOX_CONTAINER to a Linux image with util-linux `mount` "
        "(e.g. redis:7.4.2-alpine, with util-linux installed) to run the containment "
        "assertions against a real kernel. Docker on Windows is a Linux host; a "
        "privileged container is what reaches it."
    ),
)


def generated_script() -> str:
    """The provider's own launcher, with the exec replaced by assertions."""
    from forgepilot.providers.sandbox.namespace import build_argv

    argv = build_argv(
        ["true"],
        policy=SandboxExecutionPolicy(
            mode=SandboxMode.WORKSPACE_WRITE,
            workspace_root=WORKSPACE,
            session_id=SessionId("container"),
        ),
        cwd=WORKSPACE,
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


def run_in_container(script: Path) -> dict[str, str]:
    completed = subprocess.run(
        [
            "docker", "run", "--rm", "--privileged",
            "-v", f"{script}:/g.sh:ro",
            IMAGE,
            "sh", "-c", "apk add -q --no-cache util-linux 2>/dev/null; sh /g.sh",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    line = next((l for l in completed.stdout.splitlines() if l.startswith("workspace_root=")), "")
    assert line, f"the launcher produced no verdict.\nstdout: {completed.stdout}\nstderr: {completed.stderr}"
    return dict(part.split("=", 1) for part in line.split())


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
