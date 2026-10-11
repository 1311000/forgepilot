"""The tool execution pipeline and its phases.

Seven phases, in order, each with a different amount of authority::

    tool/call (durable)  ->  pre-execute (pure)  ->  approval  ->  guards
        ->  arm (privileged)  ->  execute  ->  post-execute  ->  finalize
        ->  tool/result (durable)

The split exists because a tool call is not one decision. Deciding whether a call
may happen, deciding that a human agreed, and actually doing it are three
different questions with three different answers to "who is allowed to do this",
and collapsing them is how a hook ends up with authority it was never granted.
"""

from forgepilot.runtime.tools.arm import ArmRefused, ArmScope, mint_capabilities
from forgepilot.runtime.tools.pipeline import Phase, ToolPipeline
from forgepilot.runtime.tools.pre_execute import (
    CallPreview,
    PreDecision,
    PreDecisionKind,
    PreExecuteScope,
)
from forgepilot.runtime.tools.registry import ToolRegistryPlugin

__all__ = [
    "ArmScope",
    "ArmRefused",
    "mint_capabilities",
    "Phase",
    "ToolPipeline",
    "CallPreview",
    "PreDecision",
    "PreDecisionKind",
    "PreExecuteScope",
    "ToolRegistryPlugin",
]
