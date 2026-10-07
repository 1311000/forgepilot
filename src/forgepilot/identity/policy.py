"""The authorisation decision — service name ``policy``.

**The only decision authority in the system.** Every other component either
carries a decision out or re-checks one; none makes one.

The design's first invariant lives here, and it is enforced by the *signature*
rather than by discipline: :meth:`PolicyServicePlugin.decide` takes a
``Principal``, an ``Operation`` and an optional ``ResourceRef``. There is no
parameter a model-produced value could be passed through — no ticket
understanding, no classification, no plan, no free-form dict. "The local model
proposes but never decides" is therefore a property of the type, not a rule
someone has to remember.

The runtime check is the part that matters in Python, where annotations are not
enforced: a non-``Operation`` argument is refused rather than coerced, so model
output passed by mistake fails loudly instead of being interpreted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.security import Operation, Principal

__all__ = ["PolicyDecision", "ResourceRef", "PolicyServicePlugin", "SERVICE_NAME"]

SERVICE_NAME = "policy"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    #: Escalate to a human. The decision says "a person must answer", not "yes".
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """A resource a decision is about.

    Deliberately opaque — ``kind`` and ``id`` only. A structured resource would
    invite callers to pass richer and richer objects until one of them is
    model-produced.
    """

    kind: str
    id: str


class PolicyServicePlugin(PluginService):
    """Serves ``policy``.

    Consulted in addition to the principal's grant: the grant says what the
    person may do *in general*, policy says whether this particular call is
    allowed now. Default is **deny** — an operation nobody granted is not
    permitted by omission.
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._default = PolicyDecision(str(cfg.get("default", "deny")))
        #: Operations that require a human for everyone, whatever the grant says.
        asked = cfg.get("ask_operations") or []
        self._ask_operations = frozenset(Operation(str(op)) for op in asked)

    def default(self) -> PolicyDecision:
        return self._default

    def decide(
        self,
        principal: Principal,
        operation: Operation,
        resource: ResourceRef | None = None,
    ) -> PolicyDecision:
        """Decide, or raise on anything that is not a real decision input."""
        if not isinstance(principal, Principal):
            raise TypeError(
                f"decide() requires a Principal, got {type(principal).__name__}. Model "
                f"output, ticket understandings and plans must never reach the decision "
                f"path."
            )
        if not isinstance(operation, Operation):
            raise TypeError(
                f"decide() requires an Operation, got {type(operation).__name__} "
                f"({operation!r}). If this is a classification or a plan produced by a "
                f"model, it is not a permission and must not be treated as one."
            )
        if resource is not None and not isinstance(resource, ResourceRef):
            raise TypeError(f"decide() requires a ResourceRef or None, got {type(resource).__name__}")

        if operation in self._ask_operations:
            return PolicyDecision.ASK
        return self._default
