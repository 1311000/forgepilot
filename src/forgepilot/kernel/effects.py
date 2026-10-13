"""Typed wrappers over the kernel's plugin, effect and event primitives.

Every helper here exists because the upstream Cordis/DSH documentation and the
``cordis_port`` implementation **disagree**, and we verified the port's actual
behaviour empirically (see ``tests/kernel/test_kernel_contract.py``). The
differences that matter, and that these wrappers absorb:

1. **A function plugin's ``apply`` takes TWO arguments — ``apply(ctx, config)``.**
   The DSH docs show ``apply(ctx)``. Passing a one-arg callable does *not* raise
   at registration; the fiber silently ends up ``FAILED``. :func:`assert_apply_arity`
   turns that silent failure into a loud one.

2. **``next`` is the LAST positional parameter of a waterfall listener, not the
   first.** A listener is invoked as ``listener(*pending, next)``. The DSH docs
   describe ``f(next, ...)``.

3. **``next(value)`` ignores its argument.** The port's ``next_callback(*_ignored)``
   discards everything and the terminal is invoked with no arguments at all.
   There is no forward value threading. Values travel *backward* instead: a
   listener that returns ``next()`` propagates the terminal's return value up the
   chain. Any pipeline that needs to carry a decision through a waterfall must do
   so with return values or an explicit accumulator — never by passing it to
   ``next``.

4. **``fiber.dispose()`` returns a coroutine**, and is idempotent. Always go
   through :func:`dispose_fiber`, which awaits it when needed.

5. **``ctx.effect(setup)`` runs ``setup`` immediately** and treats its *return
   value* as the disposer. ``ctx.effect(lambda: log.append(x))`` therefore
   registers ``None`` as a disposer and cleans up nothing — a mistake that looks
   like it works until teardown.

6. **Disposers run LIFO** (last registered, first disposed). We rely on this for
   dependency-ordered teardown, so it is pinned by a test.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from cordis_port import Context, Fiber
from cordis_port import Service as PluginService  # re-exported for providers
from cordis_port.utils import _new_symbol as _kernel_new_symbol

from forgepilot.kernel.errors import (
    DisposalError,
    KernelContractViolation,
    PluginContractViolation,
)

__all__ = [
    "apply_plugin",
    "make_plugin",
    "assert_apply_arity",
    "dispose_fiber",
    "await_fiber",
    "fiber_error",
    "bind_effect",
    "bind_listener",
    "bind_waterfall",
    "create_root",
    "fiber_state_name",
    "PluginService",
    "new_interface_label",
    "isolate_service",
    "isolate_services",
    "provide",
]

F = TypeVar("F", bound=Callable[..., Any])

#: The arity a function-form plugin's ``apply`` must accept: ``(ctx, config)``.
PLUGIN_APPLY_ARITY = 2


def assert_apply_arity(apply: Callable[..., Any]) -> Callable[..., Any]:
    """Return ``apply`` unchanged, or raise if it cannot accept ``(ctx, config)``.

    Guards against difference #1 above. Called by :func:`make_plugin`, so the
    common path cannot hit the silent-``FAILED`` trap.
    """
    if not callable(apply):
        raise PluginContractViolation(f"plugin apply is not callable: {apply!r}")

    try:
        sig = inspect.signature(apply)
    except (TypeError, ValueError):  # builtins / C callables: cannot introspect
        return apply

    positional = 0
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            return apply  # *args accepts anything
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1

    if positional < PLUGIN_APPLY_ARITY:
        raise PluginContractViolation(
            f"plugin apply must accept (ctx, config) — {PLUGIN_APPLY_ARITY} positional "
            f"arguments — but {getattr(apply, '__qualname__', apply)!r} accepts "
            f"{positional}. A one-argument `apply(ctx)` registers without error and "
            f"then leaves the fiber FAILED; see kernel/effects.py."
        )
    return apply


def make_plugin(
    apply: Callable[..., Any],
    *,
    inject: list[str] | dict[str, Any] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Build the dict-form plugin descriptor the kernel expects.

    Exists so ``inject`` and the ``(ctx, config)`` arity are stated in one place
    rather than re-derived at every call site.
    """
    assert_apply_arity(apply)
    descriptor: dict[str, Any] = {"apply": apply}
    if inject is not None:
        descriptor["inject"] = inject
    if name is not None:
        descriptor["name"] = name
    return descriptor


def new_interface_label(name: str) -> Any:
    """A fresh, unique interface label for ``name``.

    Interface labels are what Cordis's isolation compares, so a label must be
    **new** for each scope that should be isolated.
    """
    return _kernel_new_symbol(f"forgepilot.isolate.{name}")


def isolate_service(ctx: Context, name: str) -> Context:
    """Return a child context with its own interface label for ``name``.

    ⚠️ **Always use this rather than ``ctx.isolate(name)``.** In this kernel a
    *bare* ``isolate(name)`` inherits the parent's label for that name, so the
    labels compare equal, no boundary is detected, and lookups cross freely — it
    silently does not isolate at all. Isolation happens only when the label
    differs, which means passing a fresh one. This was measured, not read.

    The practical consequence for ForgePilot: a service provided in a request
    scope reaches the root unless the scope was opened through here, which would
    quietly turn the fail-closed property into decoration.
    """
    return ctx.isolate(name, new_interface_label(name))


def isolate_services(ctx: Context, *names: str) -> Context:
    """Isolate several service names in one branch.

    Each isolate extends the previous context, so the layering composes. Used by
    request scopes, which must isolate everything they provide — a scope that
    isolated ``constraints`` but not ``session`` would leak the second to the
    root, which is exactly the mistake the single-name helper exists to prevent.
    """
    for name in names:
        ctx = isolate_service(ctx, name)
    return ctx


def provide(ctx: Context, name: str, value: Any) -> Any:
    """Register ``name`` in ``ctx``'s own scope. Thin wrapper, kept for symmetry."""
    return ctx.provide(name, value)


def create_root() -> Context:
    """Create a root context.

    The root fiber is ACTIVE on creation and its ``runtime`` is ``None``, which
    means reads on it take the kernel's **unchecked** path (missing names return
    ``None`` rather than raising). Do not hand the root context to a tool or any
    other object that is expected to be scope-confined — see
    ``test_root_context_read_is_unchecked``.
    """
    return Context()


def fiber_state_name(fiber: Fiber) -> str:
    """Return a fiber's lifecycle state as a name, e.g. ``"ACTIVE"``.

    Prefer :func:`forgepilot.kernel.effects.dispose_fiber`-style waiting over
    polling this: the state can read ACTIVE before the plugin's ``apply`` has
    run. See ``test_active_state_does_not_imply_apply_has_run``.
    """
    return fiber.get_state().name


async def await_fiber(fiber: Fiber) -> None:
    """Wait for ``fiber`` to finish activating, **re-raising the failure it recorded**.

    A plugin whose ``apply`` raises does not raise out of ``ctx.plugin(...)``: the
    kernel keeps the exception on the fiber and leaves the state ``FAILED``. A caller
    that only reads the state therefore learns *that* a row failed and never *why*,
    which is exactly what ``--boot`` did — it printed ``FAILED`` and then
    ``result: OK``, because the reason was never raised anywhere.

    Awaiting the fiber is the port's documented way to ask (see
    ``test_active_state_does_not_imply_apply_has_run``), and the same await returns
    immediately for a fiber that activated successfully.

    ⚠️ **Only await a fiber you already know is not ``PENDING``.** A fiber that
    declares an unavailable ``inject`` stays ``PENDING`` *by design*, and awaiting it
    would wait for a dependency that is never coming. :func:`fiber_error` checks the
    state first for that reason.
    """
    await fiber


def fiber_error(fiber: Fiber) -> BaseException | None:
    """The exception a ``FAILED`` fiber recorded, or ``None``.

    Returns ``None`` for every state other than ``FAILED`` — including ``PENDING``,
    which is a documented outcome rather than a failure and must not be awaited (see
    :func:`await_fiber`). A ``FAILED`` fiber whose reason cannot be recovered also
    returns ``None``; the caller still has to treat the row as failed, because the
    state is the evidence and the message is only the explanation.

    For a **synchronous** caller (``--boot``). Inside a running event loop there is
    nothing to spin, so this raises rather than reporting a loop's own error as the
    plugin's; use ``await await_fiber(fiber)`` there instead.
    """
    if fiber_state_name(fiber) != "FAILED":
        return None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise KernelContractViolation(
            "fiber_error() is for a synchronous caller with no running event loop; "
            "inside one, `await await_fiber(fiber)` and catch the exception directly"
        )

    try:
        asyncio.run(await_fiber(fiber))
    except BaseException as exc:  # noqa: BLE001 - the failure is this function's return value
        return exc
    return None


def apply_plugin(
    ctx: Context,
    plugin: Any,
    config: Any = None,
) -> Fiber:
    """Register ``plugin`` on ``ctx``, returning its :class:`Fiber`.

    Thin wrapper purely so ``cordis_port`` stays behind this package.
    """
    return ctx.plugin(plugin, config)


async def dispose_fiber(fiber: Fiber) -> None:
    """Dispose ``fiber`` and wait for teardown to finish.

    ``Fiber.dispose`` is **not a declared method** — the kernel assigns it at
    runtime (``fiber.py`` sets it to ``_root_dispose`` on the root fiber and to an
    effect for child fibers). It returns a coroutine, and is idempotent, so
    calling this twice is safe.
    """
    dispose = getattr(fiber, "dispose", None)
    if dispose is None:
        raise DisposalError(
            f"fiber {fiber!r} has no dispose attribute; the kernel's dynamic "
            f"assignment did not happen"
        )
    result = dispose()
    if isinstance(result, Awaitable):  # the normal case: a coroutine
        await result


def bind_effect(
    ctx: Context,
    setup: Callable[[], Callable[[], Any] | None],
    label: str = "anonymous",
) -> Any:
    """Register a reversible effect — see difference #5 above.

    ``setup`` runs **immediately**; whatever it *returns* is invoked on teardown.
    Return a cleanup callable from ``setup`` or nothing will be cleaned up.
    """
    return ctx.effect(setup, label)


def bind_listener(
    ctx: Context,
    event: str,
    listener: Callable[..., Any],
    options: Any = None,
) -> Any:
    """Subscribe to ``event``, returning a ``Disposable`` that unsubscribes."""
    return ctx.on(event, listener, options)


def bind_waterfall(
    ctx: Context,
    event: str,
    *pending: Any,
    terminal: Callable[..., Any],
) -> Any:
    """Run a waterfall chain, with ``terminal`` as the innermost callback.

    Signature note (differences #2 and #3): each listener is invoked as
    ``listener(*pending, next)`` — **``next`` is last** — and ``next(value)``
    discards ``value``. To carry a result back out, have listeners
    ``return next()`` and let the return value propagate.

    ``terminal`` is called with **no arguments**; it must accept none.
    """
    return ctx.waterfall(event, *pending, terminal)
