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
import os
import shlex
import shutil
import subprocess
import tempfile
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
    ReadScope,
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

#: Top-level paths a ``workspace``-confined boundary still exposes, because a
#: command cannot run without them: the loader and the shell (``/usr``, and
#: ``/bin``/``/sbin``/``/lib*`` where those are real directories rather than
#: symlinks into ``/usr``), the configuration the toolchain reads (``/etc``),
#: devices, and the process table ``unshare --mount-proc`` just gave us.
#:
#: It is a **carve-out, and it is part of the claim**: :class:`ReadScope` says
#: reads outside the workspace *and a named toolchain set* fail, so this tuple is
#: the second half of that sentence rather than an omission.
TOOLCHAIN_ROOTS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/lib32",
    "/libx32",
    "/etc",
    "/dev",
    "/proc",
)

#: What the mask loop leaves alone: the toolchain set, plus ``/tmp``, which the
#: tmpfs rule at the end of the list owns and which must not be masked twice.
_MASK_KEEP: tuple[str, ...] = ("/tmp", *TOOLCHAIN_ROOTS)

#: Cap on how much output is kept per stream. A command that prints a gigabyte
#: must not become a gigabyte in the session log.
MAX_OUTPUT_BYTES = 64_000

#: The two halves of the read probe, and both are needed.
#:
#: ``_READ_PROBE_MARKER`` is printed by the probe command *itself*, so that "the
#: command produced nothing" is distinguishable from "the read was refused" —
#: without it, a launcher that swallows the command looks exactly like a
#: boundary that hid the file. ``_READ_PROBE_LEAK`` is written into a canary
#: **outside** the workspace: seeing it come back out means the boundary confines
#: writes and not reads, which is the whole distinction :class:`ReadScope` draws.
_READ_PROBE_MARKER = "FORGEPILOT-READ-PROBE-OK"
_READ_PROBE_LEAK = "FORGEPILOT-READ-PROBE-LEAKED"


class MountKind(StrEnum):
    BIND = "bind"
    TMPFS = "tmpfs"
    #: Replace every top-level entry the boundary does not name with a tmpfs (a
    #: directory) or ``/dev/null`` (a file). This is what a *read* claim costs: a
    #: root bound read-only is still **visible**, so the only way to make a path
    #: unreadable to a command is to put something else in its place.
    MASK_UNNAMED = "mask-unnamed"
    #: Bind ``.`` — the launcher's own cwd, taken **before** the masks go up —
    #: instead of the target path, which by then resolves to a mask.
    BIND_FROM_CWD = "bind-from-cwd"


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
    #: For :attr:`MountKind.MASK_UNNAMED`: the paths that must survive the masks.
    #: The workspace is here because it is re-bound afterwards from the launcher's
    #: cwd, which is the only reference to it that outlives the mask.
    keep: tuple[Path, ...] = ()


def mount_rules(
    policy: SandboxExecutionPolicy, *, read_scope: ReadScope = ReadScope.HOST
) -> tuple[MountRule, ...]:
    """The mounts for one execution, **in precedence order**.

    Later overrides earlier, so the sequence *is* the policy:

    1. ``/`` read-only — the broad rule, and everything a ``host`` claim needs;
    2. with ``read_scope: workspace``, every top-level entry outside the toolchain
       set is **masked**, which is what makes a read outside the workspace fail;
    3. the workspace writable, if the mode allows writing — re-bound from the
       launcher's cwd under a read claim, because by then its own path resolves to
       a mask;
    4. each protected subpath read-only **after** it, so the workspace's write
       permission does not reach them.

    Writing that as "denials go last" rather than as a list of exceptions is what
    makes a new protected path a one-line addition rather than a re-audit of the
    order.

    ``read_scope`` is a parameter rather than a field of ``policy`` on purpose: it
    says how this deployment built its boundary, which a call site handing over a
    command cannot know (see :class:`ReadScope`).
    """
    confined = read_scope is ReadScope.WORKSPACE
    rules: list[MountRule] = [MountRule(target=Path("/"), read_only=True)]

    # `danger-full-access` asks for no boundary and never reaches here through
    # `run()`, so a read claim has nothing to add to it.
    if confined and policy.mode is not SandboxMode.DANGER_FULL_ACCESS:
        rules.append(
            MountRule(
                target=Path("/"),
                read_only=False,
                kind=MountKind.MASK_UNNAMED,
                keep=(policy.workspace_root,),
            )
        )
        # The workspace is bound back from the cwd in **both** modes: under a read
        # claim its ancestors have just been masked, so the path it arrived by no
        # longer names it; in `read-only` mode it is also the one path the claim
        # still lets through.
        rules.append(
            MountRule(
                target=policy.workspace_root,
                read_only=policy.mode is SandboxMode.READ_ONLY,
                kind=MountKind.BIND_FROM_CWD,
            )
        )

    if policy.mode is SandboxMode.WORKSPACE_WRITE:
        if not confined:
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


def _mask_lines(kept: Sequence[Path]) -> list[str]:
    """Hide every top-level entry this boundary does not name.

    A read-only root is *visible* — that is finding 20 — so a path becomes
    unreadable only by putting something else in its place: an empty tmpfs over a
    directory, ``/dev/null`` over a file. **Symlinks are left alone**, because
    masking one would follow it: ``/bin -> usr/bin`` on Debian means
    ``mount -t tmpfs none /bin`` replaces ``/usr/bin``, and the shell running
    this script could no longer find ``mount``.

    ⚠️ The ``cd`` comes first, and it is load-bearing. ``mount(8)`` canonicalises
    its source argument, so once ``/srv`` is a mask ``mount --bind . /srv/ws``
    resolves ``.`` to the masked path and silently binds an empty directory over
    the workspace. A process's cwd is a dentry rather than a path, so a ``cd``
    taken *before* the masks still names the real directory afterwards.
    """
    lines: list[str] = []
    for path in kept:
        lines.append(f"cd {shlex.quote(path.as_posix())} || exit 126")
    keep = "|".join(_MASK_KEEP)
    lines.extend(
        [
            "# Mask, or the read claim stays a sentence.",
            "for entry in /*; do",
            '  if [ -L "$entry" ]; then continue; fi',
            f'  case "$entry" in {keep}) continue ;; esac',
            '  if [ -d "$entry" ]; then mount -t tmpfs none "$entry"; '
            'else mount --bind /dev/null "$entry"; fi',
            "done",
        ]
    )
    return lines


def _bind_from_cwd(quoted: str, *, read_only: bool) -> list[str]:
    """Re-bind the workspace from ``.``, and refuse to go on if that missed.

    ``mount -c`` (``--no-canonicalize``) is what keeps ``.`` a dentry instead of
    a path: without it ``mount(8)`` resolves ``.`` to the masked location and the
    workspace comes back empty, which reads as a broken checkout rather than as a
    boundary bug. The inode comparison behind it is the belt for that same
    failure, in the spirit of the ``/.forgepilot-boundary-check`` above — two ways
    to notice, because the failure is silent.
    """
    mode = "ro" if read_only else "rw"
    return [
        f"mkdir -p {quoted}",
        f"mount -c --bind . {quoted}",
        f"mount -o remount,bind,{mode} {quoted}",
        f'if [ "$(stat -c %d:%i .)" != "$(stat -c %d:%i {quoted})" ]; then '
        f'echo "refusing to run: the workspace bind landed on a mask, not on the '
        f'workspace" >&2; exit 125; fi',
    ]


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
        if rule.kind is MountKind.MASK_UNNAMED:
            lines.extend(_mask_lines(rule.keep))
            continue
        if rule.kind is MountKind.BIND_FROM_CWD:
            lines.extend(_bind_from_cwd(quoted, read_only=rule.read_only))
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
    read_scope: ReadScope = ReadScope.HOST,
) -> tuple[str, ...]:
    """The full command line that runs ``command`` inside the boundary.

    ``enter`` is how the namespace is created, as a command line rather than a
    single binary name — so ``unshare`` can be swapped for ``bwrap``, for
    ``nsenter`` into an existing namespace, or (in tests on a non-Linux host) for
    a launcher that reaches a Linux kernel some other way. Its production value
    is the default; the indirection costs one argument and makes the syscall half
    reachable from a machine that has no such syscall.

    ``read_scope`` decides whether the script only *binds* the filesystem
    read-only (``host``) or also **masks** what it does not name (``workspace``).
    """
    flags = ["--mount", "--pid", "--ipc", "--uts", "--fork", "--mount-proc"]
    if policy.network is not NetworkAccess.ALLOW:
        flags.append("--net")

    script = _mount_script(mount_rules(policy, read_scope=read_scope), cwd, command)
    # `sh -c <script> <name> <args...>`: the operand after the script is `$0` and
    # everything after that is `$1...`, which is what `exec "$@"` runs.
    #
    # ⚠️ **No `--` here.** It is tempting for symmetry with the option-parsing
    # tools above, and it is wrong: `sh -c` does not parse options after the
    # script, so `--` becomes `$1` and `exec "$@"` tries to run the option.
    # Measured, after a first version did exactly that and reported
    # `sh: exec: line 5: : Permission denied`.
    return (*enter, *flags, "/bin/sh", "-c", script, "sh", *command)


def read_scope_from(value: Any) -> ReadScope:
    """Parse a configured read claim, **loudly**.

    An unrecognised value must not fall back to a default. ``read_scope:
    workspce`` would quietly mean ``host``, and the deployment would believe it
    confines reads while the boundary claims nothing of the kind — the same
    "looks enforced, is not" shape the seam refuses by contract.
    """
    try:
        return ReadScope(str(value))
    except ValueError:
        valid = ", ".join(sorted(scope.value for scope in ReadScope))
        raise ValueError(
            f"unknown sandbox read_scope {value!r}; this boundary can claim one of: {valid}"
        ) from None


def _read_probe_verdict(exit_code: int, stdout: str) -> SandboxEnforcement:
    """What one probe command says about a ``workspace`` read claim.

    Three ways to answer, and the middle one is the point: a boundary that runs
    the command and hides nothing prints the canary back, so the claim is *not*
    met and the honest answer is ``PARTIAL`` — not ``FULL``, which is what this
    provider reported for years while every read was wide open.
    """
    if exit_code != 0:
        return SandboxEnforcement.NONE
    if _READ_PROBE_MARKER not in stdout:
        # It ran and produced no evidence either way: a launcher that swallows
        # the command, or a shell found without the toolchain that was supposed
        # to be reachable. Unverified is not verified.
        return SandboxEnforcement.PARTIAL
    if _READ_PROBE_LEAK in stdout:
        return SandboxEnforcement.PARTIAL
    return SandboxEnforcement.FULL


def _read_probe_roots() -> tuple[Path, ...]:
    """Where a read probe's canary may go, in order, and why these.

    The probe is only meaningful if the canary is somewhere the claim says is
    unreachable, so every candidate here is a path the boundary must be hiding.
    That rules out two tempting answers: the system temp directory, which is a
    **fresh tmpfs inside the boundary**, so a canary there is invisible in every
    scope and the probe would report ``FULL`` for a boundary that hides nothing;
    and anywhere inside the workspace, which the claim never covered.

    ``$HOME`` is first because on the target platforms it is a masked top-level
    entry that is neither bound nor whitelisted. ``/var/tmp`` is the same kind of
    path for a host whose home is not writable. Windows is on the list because
    this provider runs there too — its ACLs are not what the claim is about, and
    the probe answers ``NONE`` there anyway (no ``unshare``).
    """
    if os.name == "nt":
        return (Path.home(), Path(tempfile.gettempdir()))
    return (Path.home(), Path("/var/tmp"), Path("/root"))


def _read_probe_scratch() -> Path | None:
    """A writable directory the boundary must hide, or ``None`` if there is none."""
    for root in _read_probe_roots():
        try:
            return Path(tempfile.mkdtemp(prefix="forgepilot-read-probe-", dir=root))
        except OSError:
            continue
    return None


class NamespaceSandboxPlugin(PluginService):
    """Serves ``sandbox`` with Linux namespaces and read-only bind mounts.

    Config::

        enter: ["unshare"]        # how the boundary is entered; bwrap also works
        read_scope: host          # what it claims about reads (see ReadScope)
        max_output_bytes: 64000   # per stream, so one command cannot fill the log

    ``probe()`` runs at first use rather than reading a flag, and it checks the
    claim in ``read_scope`` rather than a fixed one: ``host`` is verified by a
    *write* the boundary must refuse, ``workspace`` by a *read* it must refuse.
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._enter: tuple[str, ...] = tuple(cfg.get("enter") or ("unshare",))
        self._read_scope = read_scope_from(cfg.get("read_scope", ReadScope.HOST.value))
        self.max_output_bytes = int(cfg.get("max_output_bytes", MAX_OUTPUT_BYTES))
        self._probed: SandboxEnforcement | None = None

    # -- probe -------------------------------------------------------------
    def probe(self) -> SandboxEnforcement:
        """``FULL`` only for a claim that was verified — never a guess.

        **It runs something.** The obvious implementation — "is ``unshare`` on
        PATH?" — reports ``FULL`` on a host that has the binary and cannot use
        it, which is a real configuration: Ubuntu 24.04's default AppArmor policy
        blocks unprivileged user namespaces, so ``unshare`` is present and fails.
        ``arm`` trusts this number, so a presence check would hand out ``exec``
        on the strength of a file existing.

        Cached: the answer is a fact about the machine, and a probe per tool call
        would pay a process spawn to learn something that has not changed.

        **What gets verified depends on the claim.** ``read_scope: host`` is
        checked by a *write* a read-only root must refuse; ``read_scope:
        workspace`` by a *read* it must refuse — because a write test cannot tell
        a boundary from a boundary that hides nothing, and reporting ``FULL`` for
        the second is exactly what finding 20 measured.
        """
        if self._probed is None:
            self._probed = self._probe_now()
        return self._probed

    def _probe_now(self) -> SandboxEnforcement:
        if shutil.which(self._enter[0]) is None:
            return SandboxEnforcement.NONE
        if self._read_scope is ReadScope.WORKSPACE:
            return self._probe_reads()
        return self._probe_writes()

    def _probe_writes(self) -> SandboxEnforcement:
        """The claim ``host`` makes: a read-only ``/`` refuses the write.

        Nothing here says anything about reads, and nothing here can — that is
        why there are two probes.
        """
        probe_policy = SandboxExecutionPolicy(
            mode=SandboxMode.READ_ONLY,
            workspace_root=Path("/"),
            session_id=SessionId("sandbox-probe"),
        )
        completed = self._run_probe(["true"], policy=probe_policy, cwd=Path("/"))
        if completed is None:
            return SandboxEnforcement.NONE
        return SandboxEnforcement.FULL if completed.returncode == 0 else SandboxEnforcement.NONE

    def _probe_reads(self) -> SandboxEnforcement:
        """The claim ``workspace`` makes: a read *outside* the workspace fails.

        The canary is placed outside the workspace **and outside `/tmp`** on
        purpose. Under the boundary `/tmp` is a fresh tmpfs even in the
        write-only scope, so a canary there would be invisible in *both* scopes
        and the probe would report ``FULL`` for the wrong reason — the same trap
        that hid a prepared workspace in the eval harness. The canary sits in one
        of the directories :func:`_read_probe_scratch` allows, each of which the
        boundary must be hiding; ``$HOME`` is neither the workspace nor anything
        this provider binds, so a boundary that keeps its read claim cannot hand
        the file back.

        The launcher's own boundary check runs inside this probe too (it is in
        every generated script), so a broken *write* boundary answers here as a
        non-zero exit.
        """
        scratch = _read_probe_scratch()
        if scratch is None:
            # No canary, no evidence — and no evidence must never read as FULL.
            return SandboxEnforcement.PARTIAL
        try:
            canary = scratch / "canary"
            canary.write_text(_READ_PROBE_LEAK, encoding="utf-8")
            workspace = scratch / "workspace"
            workspace.mkdir()
            policy = SandboxExecutionPolicy(
                mode=SandboxMode.WORKSPACE_WRITE,
                workspace_root=workspace,
                session_id=SessionId("sandbox-probe"),
            )
            command = [
                "sh",
                "-c",
                f"cat {shlex.quote(canary.as_posix())} 2>/dev/null; echo {_READ_PROBE_MARKER}",
            ]
            completed = self._run_probe(command, policy=policy, cwd=workspace)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if completed is None:
            return SandboxEnforcement.NONE
        return _read_probe_verdict(completed.returncode, completed.stdout or "")

    def _run_probe(
        self, command: Sequence[str], *, policy: SandboxExecutionPolicy, cwd: Path
    ) -> subprocess.CompletedProcess[str] | None:
        argv = build_argv(
            command,
            policy=policy,
            cwd=cwd,
            enter=self._enter,
            read_scope=self._read_scope,
        )
        try:
            return subprocess.run(  # noqa: S603 - entering the namespace is the point
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _refusal(self, enforcement: SandboxEnforcement) -> str:
        """Why the command was not run, in the terms of *this* provider.

        Two different failures wear the same ``126``, and conflating them sends
        the reader to the wrong fix: one is a host that cannot isolate anything,
        the other is a boundary that isolates writes and not reads while the
        deployment claims ``workspace``.
        """
        if enforcement is SandboxEnforcement.PARTIAL:
            return (
                "this deployment claims reads are confined to the workspace "
                f"(read_scope={self._read_scope.value!r}) and the probe could not "
                "verify it: a sandboxed command still reached a file outside its "
                "workspace, so this boundary confines writes and not reads. Refusing "
                "to run uncontained. Either enforce the claim or set "
                "read_scope='host' and accept that reads are not confined "
                "(finding 20, docs/notes/evals.md)."
            )
        return (
            "the Linux namespace sandbox is not available on this host "
            f"({self._enter[0]!r} missing, or it cannot create namespaces — "
            "Ubuntu 24.04's AppArmor policy blocks unprivileged user "
            "namespaces by default). Refusing to run uncontained: install a "
            "working sandbox, or use a provider that does not claim one."
        )

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

        enforcement = self.probe()
        if enforcement is not SandboxEnforcement.FULL:
            # Refused, not degraded. Running the command anyway and reporting a
            # weaker `enforcement` would be the silent fallback this provider
            # exists to avoid — the caller asked for a boundary and would not get
            # one, at a point where it has already decided it can proceed.
            return ExecResult(
                argv=argv,
                exit_code=126,
                stderr=self._refusal(enforcement),
                # What the boundary actually had, so a caller reading only the
                # result can tell "no sandbox on this host" from "a sandbox that
                # does not meet the claim this deployment made".
                enforcement=enforcement,
                executed=False,
            )

        full = build_argv(
            argv,
            policy=policy,
            cwd=cwd,
            enter=self._enter,
            read_scope=self._read_scope,
        )
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
