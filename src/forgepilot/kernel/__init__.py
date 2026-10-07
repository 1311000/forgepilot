"""Thin ForgePilot layer over the ``cordis_port`` microkernel.

**This is the only package permitted to import ``cordis_port``.** Everything
else in ForgePilot goes through the helpers re-exported here, so that a future
kernel fork or replacement is a change confined to this package.

The semantics we depend on are not assumed — they are pinned by
``tests/kernel/test_kernel_contract.py``. Several of them differ from the
upstream Cordis/DSH documentation; see ``effects.py`` for the specifics.
"""

from forgepilot.kernel.contracts import (
    assert_declares_inject,
    plugin_declares,
    requires_inject,
)
from forgepilot.kernel.effects import (
    apply_plugin,
    assert_apply_arity,
    bind_effect,
    bind_listener,
    bind_waterfall,
    create_root,
    dispose_fiber,
    fiber_state_name,
    make_plugin,
)
from forgepilot.kernel.errors import (
    ApprovalUnavailable,
    CapabilityNotGranted,
    ContractViolation,
    Denied,
    DisposalError,
    ForgePilotError,
    GuardRejected,
    KernelContractViolation,
    ModelVisibleNotLogged,
    PluginContractViolation,
)

__all__ = [
    # errors
    "ForgePilotError",
    "KernelContractViolation",
    "PluginContractViolation",
    "ContractViolation",
    "DisposalError",
    "Denied",
    "GuardRejected",
    "ApprovalUnavailable",
    "ModelVisibleNotLogged",
    "CapabilityNotGranted",
    # effects
    "apply_plugin",
    "make_plugin",
    "assert_apply_arity",
    "dispose_fiber",
    "bind_effect",
    "bind_listener",
    "bind_waterfall",
    "create_root",
    "fiber_state_name",
    # contracts
    "requires_inject",
    "assert_declares_inject",
    "plugin_declares",
]
