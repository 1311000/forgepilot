"""ForgePilot error tree.

Kept deliberately flat and closed-set where the architecture demands it —
``ApprovalOutcome``-style fail-closed reasoning depends on there being no
"other" case that quietly means *allow*.
"""

from __future__ import annotations

# Re-exported: the base lives at the top level so that `seams/` can depend on it
# without pulling in the plugin kernel. See forgepilot/errors.py.
from forgepilot.errors import ForgePilotError as ForgePilotError


# --------------------------------------------------------------------------
# Kernel / plugin contract violations
# --------------------------------------------------------------------------
class KernelContractViolation(ForgePilotError):
    """A behaviour we depend on from ``cordis_port`` did not hold.

    Raised by the guards in :mod:`forgepilot.kernel.effects` and
    :mod:`forgepilot.kernel.contracts`. If this ever fires, the kernel port has
    drifted from what ``tests/kernel/test_kernel_contract.py`` pins — stop and
    re-evaluate before trusting anything built on top.
    """


class PluginContractViolation(KernelContractViolation):
    """A plugin was declared or registered in a way the runtime cannot honour."""


class ContractViolation(ForgePilotError):
    """A seam implementation did not satisfy its declared Protocol."""


class DisposalError(ForgePilotError):
    """A resource failed to release during teardown."""


# --------------------------------------------------------------------------
# Security vocabulary.
#
# Permission, Approval, Guard and Sandbox are four *distinct* concepts and must
# never be conflated. These four error types keep them apart in code:
#   - Denied               -> policy said no
#   - GuardRejected        -> a monotonic, non-reorderable invariant said no
#   - ApprovalUnavailable  -> an `ask` was never answered (fail-closed)
#   - CapabilityNotGranted -> a hook reached for authority it was never handed
# --------------------------------------------------------------------------
class Denied(ForgePilotError):
    """Permission policy refused the operation."""


class GuardRejected(ForgePilotError):
    """A monotonic guard rejected the operation. Later phases cannot override."""


class ApprovalUnavailable(ForgePilotError):
    """An approval request had no answerer. Fail-closed: treated as refusal."""


class CapabilityNotGranted(ForgePilotError):
    """Code attempted an effect it holds no capability token for.

    This is the error that makes DSH defect #1863 structurally impossible: a
    ``tools/pre-execute`` hook is handed a scope with no filesystem, process or
    network authority, so a side effect attempted before approval fails here.
    """


class ModelVisibleNotLogged(ForgePilotError):
    """Content would have reached the model without first entering the session log.

    Enforces the ``model-visible means logged`` invariant. Raised at the single
    boundary that builds a model request, so it is a hard failure rather than a
    warning.
    """


__all__ = [
    "ForgePilotError",
    "KernelContractViolation",
    "PluginContractViolation",
    "ContractViolation",
    "DisposalError",
    "Denied",
    "GuardRejected",
    "ApprovalUnavailable",
    "CapabilityNotGranted",
    "ModelVisibleNotLogged",
]
