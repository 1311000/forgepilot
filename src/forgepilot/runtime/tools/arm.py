"""The arm phase: the first and only place a call may reach for authority.

Everything before this point is decisions. Everything after it is effects. This
is the seam between them, and it is deliberately the **narrowest** phase: it runs
after the human has been asked and after the guards have had their say, so by the
time anything here acquires a resource, the question of whether it should has
already been settled.

That ordering is what makes the reference implementation's defect impossible
here. There is no hook before approval that can execute anything, because the
only phase that hands out capabilities runs after it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forgepilot.errors import ForgePilotError
from forgepilot.seams.refs import SessionId
from forgepilot.seams.sandbox import SandboxEnforcement
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess
from forgepilot.seams.tools import Capabilities, ToolEffect

__all__ = ["ArmRefused", "ArmScope", "mint_capabilities", "enforcement_rank"]


class ArmRefused(ForgePilotError):
    """Authority could not be granted for this call.

    Distinct from a guard denial: a denial is a policy judgement, this is the
    environment being unable to support what the call needs. Failing here is
    fail-closed — an unenforceable sandbox is not a sandbox.
    """


_ORDER = {
    SandboxEnforcement.NONE: 0,
    SandboxEnforcement.PARTIAL: 1,
    SandboxEnforcement.FULL: 2,
}


def enforcement_rank(level: SandboxEnforcement) -> int:
    return _ORDER[level]


@dataclass(frozen=True, slots=True)
class ArmScope:
    """The context in which capabilities are minted for one call."""

    session_id: SessionId
    constraints: ExecutionConstraints
    enforcement: SandboxEnforcement
    workspace_root: Path | None = None


def mint_capabilities(
    *,
    effects: frozenset[ToolEffect],
    scope: ArmScope,
    minimum_enforcement_for_exec: SandboxEnforcement = SandboxEnforcement.FULL,
) -> Capabilities:
    """Compute what this call actually gets: the intersection, not any one input.

    Three independent limits, all of which must permit a capability for it to be
    granted:

    - what the tool **declared** it needs (its effects),
    - what the caller's **constraints** allow (network, an actual writable set),
    - what the host can **enforce** (a sandbox that reports less than the
      required level cannot be asked to contain a subprocess).

    The last one is the interesting case. ``EXEC`` is refused outright when
    enforcement is weaker than
    ``minimum_enforcement_for_exec`` (``FULL`` by default), rather than granted
    and hoped about. On a host that reports ``PARTIAL`` — including Windows, where
    the ACL restricted-token backend is honest about being partial — shell tools
    therefore refuse to run. That is the correct outcome: a profile that needs
    containment should fail loudly instead of quietly running uncontained.
    Lowering the requirement is possible and is a deliberate, visible act.
    """
    granted = Capabilities.for_effects(effects)

    if granted.network and scope.constraints.network is not NetworkAccess.ALLOW:
        granted = granted.without("network")

    if granted.fs_write and not scope.constraints.writable_paths:
        granted = granted.without("fs_write")

    if granted.exec:
        if enforcement_rank(scope.enforcement) < enforcement_rank(
            minimum_enforcement_for_exec
        ):
            raise ArmRefused(
                f"this call declares exec but the host reports sandbox enforcement "
                f"{scope.enforcement.value!r}, which is weaker than the required "
                f"{minimum_enforcement_for_exec.value!r}. Refusing rather than running "
                f"uncontained; lower minimum_enforcement_for_exec explicitly if that is "
                f"what you intend."
            )

    return granted
