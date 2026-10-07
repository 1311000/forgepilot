"""Plugin-contract helpers.

The important one is :func:`assert_declares_inject`, which guards a trap we
verified in the kernel and that the design's fail-closed argument depends on
being handled correctly.

**The trap.** In ``ReflectService.handler_get`` the lookup raises
``cannot get property "X" without inject`` only under specific conditions. When
the defining site's fiber has no runtime, the port takes an unchecked path::

    def_site = ctx.__get_symbol__(symbols.shadow) or ctx
    if def_site.fiber.runtime is None:
        return ctx.reflect.get(prop, False)     # non-strict -> returns None

So a plugin that reads a service it never declared can silently receive
``None`` instead of an error. The *real* fail-closed mechanism is not the read
raising — it is that **a fiber which declares ``inject`` stays ``PENDING`` and
never activates while a dependency is unavailable**, and that a declared-but-
unavailable dependency raises ``cannot get required service "X" in inactive
context``.

The consequence for ForgePilot: a plugin that needs ``constraints`` (or any
other security-relevant service) **must declare it in ``inject``**. Reading it
without declaring it is not a permission check. Hence this module — plus the
planned import-graph test in ``tests/contract`` — enforce the declaration
statically.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from forgepilot.kernel.errors import PluginContractViolation

__all__ = [
    "PluginLike",
    "requires_inject",
    "plugin_declares",
    "assert_declares_inject",
]


@runtime_checkable
class PluginLike(Protocol):
    """Anything the kernel will accept as a plugin.

    Function plugins, objects with an ``apply`` attribute, classes, and dict
    descriptors all qualify.
    """

    def apply(self, ctx: Any, config: Any) -> Any: ...  # pragma: no cover


def requires_inject(*names: str) -> Any:
    """Decorator recording the services a plugin requires.

    Attaches ``inject`` to the callable, matching the attribute form the kernel
    reads (``getattr(plugin, "inject", None)``).
    """

    def decorate(target: Any) -> Any:
        existing = set(plugin_declares(target))
        target.inject = sorted(existing | set(names))
        return target

    return decorate


def plugin_declares(plugin: Any) -> set[str]:
    """Return the service names ``plugin`` declares in its ``inject``.

    Handles all three declaration forms the kernel normalises: the dict
    descriptor, the class/instance attribute, and the bare callable attribute.
    ``inject`` may be a list of names or a mapping of name to options.
    """
    raw: Any = None
    if isinstance(plugin, dict):
        raw = plugin.get("inject")
    else:
        raw = getattr(plugin, "inject", None)

    if raw is None:
        return set()
    if isinstance(raw, dict):
        return set(raw.keys())
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, Iterable):
        return {str(item) for item in raw}
    return set()


def assert_declares_inject(plugin: Any, required: Iterable[str]) -> None:
    """Raise unless ``plugin`` declares every name in ``required``.

    Call this at registration time for any plugin that touches a
    security-relevant service. It cannot stop a determined plugin from reading
    an undeclared service, but it makes the omission a loud failure at load
    rather than a silent ``None`` at the moment authority is being decided.
    """
    required_set = set(required)
    declared = plugin_declares(plugin)
    missing = required_set - declared
    if missing:
        raise PluginContractViolation(
            f"plugin {getattr(plugin, '__name__', type(plugin).__name__)!r} does not "
            f"declare inject for {sorted(missing)!r}; it declares {sorted(declared)!r}. "
            f"An undeclared service read can silently return None rather than raising, "
            f"so security-relevant dependencies must be declared. "
            f"See kernel/contracts.py."
        )
