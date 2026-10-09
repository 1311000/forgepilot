"""Real containment on Linux, built from namespaces and mounts.

The other providers bracket this one: `recording` never executes, `local`
executes with no boundary. This is the first that does both, and therefore the
first that can honestly answer :attr:`SandboxEnforcement.FULL` — which matters
because `arm` refuses `exec` on any host reporting below `full`. With it, `run`
and the project's own checks work in a deployment without
``--allow-uncontained-exec``.

**Two stages, and the order is not negotiable.** Privileged setup happens first,
inside the new namespace: bind ``/`` read-only, bind the workspace writable on
top, put ``.git`` back to read-only, give ``/tmp`` a fresh tmpfs. Only then does
the command run, via ``exec`` — replacing the launcher rather than nesting under
it. Tightening before setting up would make the mounts impossible; Codex's
``codex-linux-sandbox`` splits the same way (an outer bubblewrap stage, an inner
seccomp stage, then ``execvp``).

**The logic here is pure.** :func:`mount_rules` and :func:`build_argv` decide
everything and are tested on any platform; only :meth:`run` touches syscalls.
That split is deliberate — "written but never run" is how this project has found
every one of its worst defects, so the part that *can* be decided in a test is
not left inside the part that cannot.

**Why not bubblewrap.** It is nicer to write against and it is what Claude Code
uses — but it has to be *installed*, and a sandbox provider that cannot run on a
stock host is not a boundary a deployment has. ``unshare`` ships with util-linux.
bwrap can replace :func:`build_argv` later without touching anything else, which
is what the seam is for.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.refs import SessionId
from forgepilot.seams.sandbox import (
    SERVICE_NAME,
    ExecResult,
    SandboxEnforcement,
    SandboxExecutionPolicy,
    SandboxMode,
)
from forgepilot.seams.security import NetworkAccess

__all__ = [
    "NamespaceSandboxPlugin",
    "MountKind",
    "MountRule",
    "build_argv",
    "mount_rules",
]

#: Paths that stay read-only even inside a writable workspace.
#:
#: ``.git`` because a hook written there runs on the next ``git commit`` — outside
#: the sandbox, with the user's privileges, so a sandboxed write becomes an
#: unsandboxed execution. The others are the same shape: agent configuration that
#: something else reads and acts on.
PROTECTED_SUBPATHS: tuple[str, ...] = (".git", ".codex", ".claude", ".agents")

#: Cap on how much output is kept per stream. A command that prints a gigabyte
#: must not become a gigabyte in the session log.
MAX_OUTPUT_BYTES = 64_000


class MountKind(StrEnum):
    BIND = "bind"
    TMPFS = "tmpfs"


@dataclass(frozen=True, slots=True)
class MountRule:
    """One mount, and whether it is read-only afterwards."""

    target: Path
    read_only: bool
    kind: MountKind = MountKind.BIND
    #: Skip the rule when the path does not exist.
    #:
    #: Protected subpaths need this and nothing else does. A repository has
    #: ``.git``; it almost never has ``.codex``, ``.claude`` and ``.agents`` as
    #: well. Binding a path that is not there fails, and with ``set -e`` that
    #: failure aborts the whole launcher — so a rule meant to *protect* something
    #: would instead make the sandbox refuse to run at all, on nearly every
    #: repository. Measured: the first traced run stopped at
    #: ``mount: /ws/.codex: mount point does not exist``.
    only_if_present: bool = False


def mount_rules(policy: SandboxExecutionPolicy) -> tuple[MountRule, ...]:
    """The mounts for one execution, **in precedence order**.

    Later overrides earlier, so the sequence *is* the policy:

    1. ``/`` read-only — the broad rule;
    2. the workspace writable, if the mode allows writing;
    3. each protected subpath read-only **after** it, so the workspace's write
       permission does not reach them.

    Writing that as "denials go last" rather than as a list of exceptions is what
    makes a new protected path a one-line addition rather than a re-audit of the
    order.
    """
    rules: list[MountRule] = [MountRule(target=Path("/"), read_only=True)]

    if policy.mode is SandboxMode.WORKSPACE_WRITE:
        rules.append(MountRule(target=policy.workspace_root, read_only=False))
        for name in PROTECTED_SUBPATHS:
            rules.append(
                MountRule(
                    target=policy.workspace_root / name,
                    read_only=True,
                    only_if_present=True,
                )
            )

    # Always last, and always writable: a read-only root leaves compilers and
    # test runners with nowhere to write, and giving them the real `/tmp` would
    # let one command read another's leftovers.
    rules.append(MountRule(target=Path("/tmp"), read_only=False, kind=MountKind.TMPFS))
    return tuple(rules)


def _mount_script(rules: Sequence[MountRule], cwd: Path, argv: Sequence[str]) -> str:
    """The launcher: do the mounts, become the command.

    ``exec`` rather than a nested call, so the sandboxed process does not sit
    under a shell that could be signalled into doing something else — and so
    there is no extra process holding the namespace open after the command exits.
    """
    lines = [
        # `set -e` so a mount that fails **stops the script**. Without it, a bind
        # that did not take effect is followed by the command running anyway —
        # uncontained, while `probe()` has already reported `FULL` and `arm` has
        # already allowed `exec`. Nothing downstream could tell.
        "set -e",
        # A private mount table, or the bind mounts would propagate to the host.
        "mount --make-rprivate / 2>/dev/null || true",
    ]
    for rule in rules:
        # `as_posix`, not `str`: these paths are resolved **inside the Linux
        # namespace**, where the separator is always `/`. Using the build host's
        # would emit `\tmp` on a Windows checkout — a path that does not exist
        # there, from a script that also does not run there. The convention
        # belongs to the target, so it is stated rather than inherited.
        quoted = shlex.quote(rule.target.as_posix())
        if rule.kind is MountKind.TMPFS:
            lines.append(f"mount -t tmpfs none {quoted}")
            continue
        bind = [f"mount --bind {quoted} {quoted}"]
        # ⚠️ **Every** bind states its mode explicitly, including the writable
        # ones. This is not symmetry for its own sake: after `/` has been
        # remounted read-only, a *later* bind of a subdirectory inherits the
        # read-only flag, so the workspace would come out unwritable and every
        # write the agent makes — every test fixture, every edit — would fail
        # with `Read-only file system`.
        #
        # That was the shipped behaviour until this was run in a container and
        # measured, and no unit test caught it: the tests asserted the *rule
        # list*, and never that the sequence produces a writable workspace.
        mode = "ro" if rule.read_only else "rw"
        bind.append(f"mount -o remount,bind,{mode} {quoted}")

        if rule.only_if_present:
            lines.append(f"if [ -e {quoted} ]; then " + " && ".join(bind) + "; fi")
        else:
            lines.extend(bind)

    # A positive check that the boundary exists, before handing over control.
    # `set -e` catches a mount that failed; this catches one that "succeeded"
    # without taking effect — busybox's `mount` remounts a bind to nothing and
    # returns failure, but a different tool might return success.
    checker = shlex.quote("/.forgepilot-boundary-check")
    lines.append(
        f"if ( : > {checker} ) 2>/dev/null; then "
        f"rm -f {checker} 2>/dev/null; "
        f'echo "refusing to run: the boundary did not take effect; / is writable" >&2; '
        f"exit 125; "
        f"fi"
    )

    lines.append(f"cd {shlex.quote(cwd.as_posix())} || exit 126")
    lines.append('exec "$@"')
    return "\n".join(lines)


def build_argv(
    command: Sequence[str],
    *,
    policy: SandboxExecutionPolicy,
    cwd: Path,
    enter: Sequence[str] = ("unshare",),
) -> tuple[str, ...]:
    """The full command line that runs ``command`` inside the boundary.

    ``enter`` is how the namespace is created, as a command line rather than a
    single binary name — so ``unshare`` can be swapped for ``bwrap``, for
    ``nsenter`` into an existing namespace, or (in tests on a non-Linux host) for
    a launcher that reaches a Linux kernel some other way. Its production value
    is the default; the indirection costs one argument and makes the syscall half
    reachable from a machine that has no such syscall.
    """
    flags = ["--mount", "--pid", "--ipc", "--uts", "--fork", "--mount-proc"]
    if policy.network is not NetworkAccess.ALLOW:
        flags.append("--net")

    script = _mount_script(mount_rules(policy), cwd, command)
    # `sh -c <script> <name> <args...>`: the operand after the script is `$0` and
    # everything after that is `$1...`, which is what `exec "$@"` runs.
    #
    # ⚠️ **No `--` here.** It is tempting for symmetry with the option-parsing
    # tools above, and it is wrong: `sh -c` does not parse options after the
    # script, so `--` becomes `$1` and `exec "$@"` tries to run the option.
    # Measured, after a first version did exactly that and reported
    # `sh: exec: line 5: : Permission denied`.
    return (*enter, *flags, "/bin/sh", "-c", script, "sh", *command)


class NamespaceSandboxPlugin(PluginService):
    """Serves ``sandbox`` with Linux namespaces and read-only bind mounts.

    Config::

        enter: ["unshare"]     # how the boundary is entered; bwrap also works
        probe_command: true    # actually try it at first use (default)
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._enter: tuple[str, ...] = tuple(cfg.get("enter") or ("unshare",))
        self.max_output_bytes = int(cfg.get("max_output_bytes", MAX_OUTPUT_BYTES))
        self._probed: SandboxEnforcement | None = None

    # -- probe -------------------------------------------------------------
    def probe(self) -> SandboxEnforcement:
        """``FULL``, ``NONE``, and never a guess.

        **It runs something.** The obvious implementation — "is ``unshare`` on
        PATH?" — reports ``FULL`` on a host that has the binary and cannot use
        it, which is a real configuration: Ubuntu 24.04's default AppArmor policy
        blocks unprivileged user namespaces, so ``unshare`` is present and fails.
        ``arm`` trusts this number, so a presence check would hand out ``exec``
        on the strength of a file existing.

        Cached: the answer is a fact about the machine, and a probe per tool call
        would pay a process spawn to learn something that has not changed.
        """
        if self._probed is None:
            self._probed = self._probe_now()
        return self._probed

    def _probe_now(self) -> SandboxEnforcement:
        import shutil

        if shutil.which(self._enter[0]) is None:
            return SandboxEnforcement.NONE

        probe_policy = SandboxExecutionPolicy(
            mode=SandboxMode.READ_ONLY,
            workspace_root=Path("/"),
            session_id=SessionId("sandbox-probe"),
        )
        argv = build_argv(["true"], policy=probe_policy, cwd=Path("/"), enter=self._enter)
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return SandboxEnforcement.NONE
        return SandboxEnforcement.FULL if completed.returncode == 0 else SandboxEnforcement.NONE

    # -- run ---------------------------------------------------------------
    async def run(
        self,
        argv: Sequence[str],
        *,
        policy: SandboxExecutionPolicy,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: str | None = None,
    ) -> ExecResult:
        argv = tuple(str(a) for a in argv)
        started = time.monotonic()

        if policy.mode is SandboxMode.DANGER_FULL_ACCESS:
            # The one mode that asks for no boundary. Reported as such rather
            # than dressed up: `enforcement` says what this execution had.
            return await self._unsandboxed(argv, policy, cwd, env, timeout_s, stdin, started)

        if self.probe() is not SandboxEnforcement.FULL:
            # Refused, not degraded. Running the command anyway and reporting a
            # weaker `enforcement` would be the silent fallback this provider
            # exists to avoid — the caller asked for a boundary and would not get
            # one, at a point where it has already decided it can proceed.
            return ExecResult(
                argv=argv,
                exit_code=126,
                stderr=(
                    "the Linux namespace sandbox is not available on this host "
                    f"({self._enter[0]!r} missing, or it cannot create namespaces — "
                    "Ubuntu 24.04's AppArmor policy blocks unprivileged user "
                    "namespaces by default). Refusing to run uncontained: install a "
                    "working sandbox, or use a provider that does not claim one."
                ),
                enforcement=SandboxEnforcement.NONE,
                executed=False,
            )

        full = build_argv(argv, policy=policy, cwd=cwd, enter=self._enter)
        return await self._spawn(full, argv, policy, cwd, env, timeout_s, stdin, started)

    async def _unsandboxed(self, argv, policy, cwd, env, timeout_s, stdin, started) -> ExecResult:
        return await self._spawn(
            argv, argv, policy, cwd, env, timeout_s, stdin, started, sandboxed=False
        )

    async def _spawn(
        self,
        full_argv: Sequence[str],
        argv: Sequence[str],
        policy: SandboxExecutionPolicy,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: str | None,
        started: float,
        *,
        sandboxed: bool = True,
    ) -> ExecResult:
        def _spawn() -> subprocess.CompletedProcess:
            return subprocess.run(  # noqa: S603 - entering the namespace is the point
                list(full_argv),
                cwd=str(cwd),
                env=dict(env),
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                shell=False,
                check=False,
            )

        try:
            completed = await asyncio.to_thread(_spawn)
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                argv=argv,
                exit_code=-1,
                stdout=_bound(_text(exc.stdout), self.max_output_bytes),
                stderr=_bound(_text(exc.stderr), self.max_output_bytes),
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
                enforcement=SandboxEnforcement.FULL if sandboxed else SandboxEnforcement.NONE,
            )
        except FileNotFoundError:
            return ExecResult(
                argv=argv,
                exit_code=127,
                stderr=f"command not found: {full_argv[0]!r}",
                enforcement=SandboxEnforcement.NONE,
                executed=False,
            )

        return ExecResult(
            argv=argv,
            exit_code=completed.returncode,
            stdout=_bound(completed.stdout or "", self.max_output_bytes),
            stderr=_bound(completed.stderr or "", self.max_output_bytes),
            duration_ms=int((time.monotonic() - started) * 1000),
            # What this execution actually had, not what the policy asked for.
            enforcement=SandboxEnforcement.FULL if sandboxed else SandboxEnforcement.NONE,
            executed=True,
        )


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _bound(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n… [{len(text) - limit} bytes omitted] …\n{text[-half:]}"
