"""Sandbox seam — service name ``sandbox``.

Sandbox and *approval* are separate concepts and must not be conflated: an
approval does not remove the sandbox boundary, and a sandbox is an environment
rather than a decision.

``probe()`` reports how strong the host's enforcement actually is. That number is
a fact about the machine, not a preference — a profile requiring real containment
must refuse to run where enforcement is weaker than it wants. On Windows the ACL
restricted-token backend reports ``PARTIAL``; Linux with bubblewrap/Landlock can
report ``FULL``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from forgepilot.errors import ForgePilotError
from forgepilot.seams.refs import SessionId
from forgepilot.seams.security import NetworkAccess

__all__ = [
    "SERVICE_NAME",
    "PolicyNotEnforceable",
    "SandboxMode",
    "SandboxEnforcement",
    "SandboxExecutionPolicy",
    "ExecResult",
    "Sandbox",
]

SERVICE_NAME = "sandbox"


class PolicyNotEnforceable(ForgePilotError):
    """A policy asked for a restriction this backend cannot apply.

    A contract of the seam, not of one provider: **a backend must refuse a policy
    it cannot enforce rather than accept it and ignore it.** Ignoring is the worse
    failure — the caller has stated an expectation, the backend has silently not
    met it, and nothing in the result says so unless the caller happens to read
    ``enforcement``. Refusing is the same shape as the seam's other fail-closed
    rules: an unenforceable boundary is not a boundary.
    """


class ReadScope(StrEnum):
    """What a boundary claims about **reads** — which is not what "read-only" says.

    Every backend here confines *writes*; that is what a read-only root is for,
    and it is not the same property. A root bound read-only is still **visible**:
    a sandboxed command can read every file the filesystem will hand it,
    including the corpus an evaluation is measuring and the checkout of a project
    the agent was never asked to touch.

    Measured on Linux against the shipped rules: inside the boundary,
    ``cat /eval/evals/tasks/not-in-the-checkout/index.json`` **succeeded and
    printed the task**, in the same namespace where writing to that directory
    returned ``Read-only file system`` (``docs/notes/evals.md``, finding 20).

    So the scope states the claim, and the default is the smaller one:

    - ``HOST`` — writes are confined; reads reach the whole filesystem. This is
      what a read-only root actually buys, and claiming more would be the defect
      this vocabulary exists to prevent.
    - ``WORKSPACE`` — reads outside the workspace and a named toolchain set fail
      as well.

    **It belongs to the boundary, not to the call.** A call site knows what it is
    about to run; it does not know how a deployment built its sandbox, so this is
    provider configuration. :meth:`Sandbox.probe` is where the claim is checked
    against the machine, and a backend configured for ``WORKSPACE`` that cannot
    meet it reports :attr:`SandboxEnforcement.PARTIAL`, never ``FULL``.
    """

    HOST = "host"
    WORKSPACE = "workspace"


class SandboxMode(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    #: Dev-only escape hatch. Never selected for a model-initiated call.
    DANGER_FULL_ACCESS = "danger-full-access"


class SandboxEnforcement(StrEnum):
    #: The platform enforces the boundary (e.g. Linux namespaces/Landlock).
    FULL = "full"
    #: Some boundary exists but is known not to be complete (e.g. Windows ACL
    #: restricted token) — or the platform reports nothing about network/process
    #: visibility, which this vocabulary does not cover at all, or the boundary
    #: confines writes but not reads while claiming :attr:`ReadScope.WORKSPACE`.
    PARTIAL = "partial"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class SandboxExecutionPolicy:
    mode: SandboxMode
    workspace_root: Path
    session_id: SessionId
    network: NetworkAccess = NetworkAccess.DENY


@dataclass(frozen=True, slots=True)
class ExecResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    #: Enforcement actually applied to *this* execution, so a caller can tell
    #: whether its assumption held rather than assuming the policy was honoured.
    enforcement: SandboxEnforcement = SandboxEnforcement.NONE
    #: Whether the command actually ran.
    #:
    #: A recording sandbox answers from a script without spawning anything, and
    #: reporting that as ``exit_code=0`` reads as "the command succeeded" when
    #: nothing happened at all. That is worse than an error: it makes a model
    #: confidently summarise the output of a command that never ran. Callers
    #: must surface ``False`` to the model rather than formatting it as success.
    executed: bool = True


@runtime_checkable
class Sandbox(Protocol):
    def probe(self) -> SandboxEnforcement: ...

    async def run(
        self,
        argv: Sequence[str],
        *,
        policy: SandboxExecutionPolicy,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: str | None = None,
    ) -> ExecResult: ...
