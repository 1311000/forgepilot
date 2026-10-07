"""The per-request scope: where constraints live, and where they stop.

This is the piece that makes authority *fail closed by construction* rather than
by everyone remembering to pass it.

A scope is a child context whose ``constraints`` service has its **own interface
label**. Because the kernel compares labels when deciding whether a lookup may
cross a boundary, a service provided in the scope is invisible from the root, and
a service provided at the root is invisible from the scope under that name. The
consequence is the property we want:

    **a plugin that declares ``inject = ["constraints"]`` stays PENDING unless it
    is registered inside a request scope.**

Not "raises when it reads them", not "gets None" — never activates at all. A
plugin that needs authority cannot be loaded in a context where authority does
not exist, so forgetting to establish one produces a plugin that does nothing
rather than a plugin that acts with no limits.

⚠️ **The label is why ``isolate_service`` exists.** A bare ``ctx.isolate(name)``
inherits the parent's label, the labels compare equal, no boundary is detected,
and the service leaks to the root — silently. That was measured, not read; see
``kernel/effects.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forgepilot.identity.signing import verify
from forgepilot.kernel import isolate_service, provide
from forgepilot.runtime.guards import GuardRegistry, UsageTracker, default_guards
from forgepilot.seams.security import ExecutionConstraints

__all__ = ["RequestScope", "open_request_scope"]

#: The service name whose interface label is isolated per request.
CONSTRAINTS = "constraints"


@dataclass
class RequestScope:
    """One request's authority, and the context that carries it."""

    ctx: Any
    constraints: ExecutionConstraints
    guards: GuardRegistry
    tracker: UsageTracker
    _closed: bool = field(default=False, repr=False)

    def close(self) -> None:
        """Release the scope. Idempotent.

        Closing does not dispose plugins registered on the scope — the caller
        owns those, and Cordis disposes them with the child fiber. What closing
        marks is that this scope must not be used again.
        """
        self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed

    def assert_open(self) -> None:
        if self._closed:
            raise RuntimeError(
                "this request scope is closed; reusing it would carry one request's "
                "authority into another"
            )


def open_request_scope(
    root: Any,
    constraints: ExecutionConstraints,
    *,
    key: bytes | None = None,
    verify_signature: bool = True,
    tracker: UsageTracker | None = None,
) -> RequestScope:
    """Open an isolated scope carrying ``constraints``.

    The signature is verified **before** the scope exists, so a forged or unsigned
    set never gets a context to live in. Verification is on by default and turning
    it off is a visible argument at the call site.
    """
    if verify_signature:
        verify(constraints, key)

    ctx = isolate_service(root, CONSTRAINTS)
    provide(ctx, CONSTRAINTS, constraints)

    usage = tracker or UsageTracker()
    guards = GuardRegistry()
    for name, guard in default_guards(usage):
        guards.register(name, guard)

    return RequestScope(ctx=ctx, constraints=constraints, guards=guards, tracker=usage)
