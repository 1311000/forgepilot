"""Kernel conformance suite — the M0 gate.

Every assertion here corresponds to a behaviour ForgePilot actually depends on.
If one of these fails, the kernel has drifted and nothing built on top is
trustworthy: **stop and re-evaluate the kernel choice rather than adapting the
tests.** See ``plans/`` for the reasoning and ``kernel/effects.py`` for the
enumerated differences between the port and the upstream docs.

Several assertions here deliberately pin behaviour that *contradicts* the
Cordis/DeepSeek Harness documentation. They are written down so the next reader
does not have to rediscover them.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from cordis_port import Context, Service

from forgepilot.kernel import (
    assert_apply_arity,
    assert_declares_inject,
    bind_effect,
    bind_waterfall,
    dispose_fiber,
    make_plugin,
    plugin_declares,
)
from forgepilot.kernel.errors import PluginContractViolation


# ---------------------------------------------------------------------------
# 1. Service construction registers
# ---------------------------------------------------------------------------
def test_service_construction_registers(root: Context, service_factory) -> None:
    """Constructing a ``Service`` subclass is what registers it. No separate call."""
    Counter = service_factory("counter", n=0)
    service = Counter(root)

    assert isinstance(service, Service)
    assert root.counter is not None
    assert root.counter.n == 0
    # The service is addressable as an interface, which is what the scoping
    # machinery keys on when propagating constraints into child contexts.
    assert root.interface("counter") is not None


# ---------------------------------------------------------------------------
# 2. `inject` gates activation — the real fail-closed mechanism
# ---------------------------------------------------------------------------
async def test_inject_gates_activation(root: Context, service_factory) -> None:
    """A plugin whose dependency is absent stays PENDING and never runs."""
    ran: list[str] = []

    def apply(ctx, config):  # noqa: ANN001, ARG001
        ran.append("ran")

    fiber = root.plugin(make_plugin(apply, inject=["dep"]))

    assert fiber.get_state().name == "PENDING"
    assert ran == [], "apply must not run while a declared dependency is missing"

    service_factory("dep")(root)
    await fiber  # activation is deferred; the fiber is awaitable

    assert fiber.get_state().name == "ACTIVE"
    assert ran == ["ran"]


async def test_active_state_does_not_imply_apply_has_run(root: Context) -> None:
    """``get_state() == ACTIVE`` is NOT a reliable signal that ``apply`` completed.

    Observed directly: immediately after ``ctx.plugin(...)`` the fiber can already
    report ACTIVE while its ``apply`` has not executed yet — the state machine
    settles ahead of the plugin body, which runs behind the fiber's ``inertia``
    awaitable.

    This matters because it makes state polling unsound. The only correct way to
    know a plugin is loaded is to ``await`` its fiber. Harness code that registers
    plugins mid-turn (the tool pipeline, per-request scopes) must await, not poll.
    """
    ran: list[str] = []
    fiber = root.plugin(make_plugin(lambda ctx, cfg: ran.append("ran")))

    assert ran == [], "apply has not run yet at this point"

    await fiber

    assert fiber.get_state().name == "ACTIVE"
    assert ran == ["ran"], "awaiting the fiber is what guarantees apply has run"


# ---------------------------------------------------------------------------
# 3. A function plugin's apply takes (ctx, config) — NOT apply(ctx)
# ---------------------------------------------------------------------------
async def test_function_plugin_apply_arity_is_ctx_and_config(root: Context) -> None:
    """The port calls ``apply(ctx, config)``; a one-arg ``apply`` silently fails.

    The DSH docs show ``apply(ctx)``. Registering a one-arg callable does not
    raise — the fiber just ends up FAILED — so ``make_plugin`` rejects it early.
    """
    with pytest.raises(PluginContractViolation, match="must accept"):
        make_plugin(lambda ctx: None)

    seen: list[tuple] = []

    def good_apply(ctx, config):  # noqa: ANN001
        seen.append((ctx, config))

    fiber = root.plugin(make_plugin(good_apply))
    await fiber
    assert fiber.get_state().name == "ACTIVE"
    assert len(seen) == 1

    # And confirm the port really does pass two arguments.
    assert len(inspect.signature(good_apply).parameters) == 2


# ---------------------------------------------------------------------------
# 4/5. Effects: setup runs immediately, return value is the disposer, LIFO order
# ---------------------------------------------------------------------------
async def test_effect_setup_runs_now_and_return_value_disposes(root: Context) -> None:
    """``ctx.effect(setup)`` calls ``setup`` now and disposes with its *return*."""
    order: list[str] = []

    def setup():
        order.append("setup")
        return lambda: order.append("cleanup")

    fiber = root.plugin(make_plugin(lambda ctx, cfg: bind_effect(ctx, setup)))
    await fiber

    assert order == ["setup"], "setup runs at registration time, not at teardown"

    await dispose_fiber(fiber)
    assert order == ["setup", "cleanup"]


async def test_effects_dispose_lifo(root: Context) -> None:
    """Disposers run last-registered-first."""

    def apply(ctx, config):  # noqa: ANN001, ARG001
        def make(name: str):
            def setup():
                return lambda: order.append(name)

            return setup

        for name in ("a", "b", "c"):
            bind_effect(ctx, make(name), label=name)

    order: list[str] = []
    fiber = root.plugin(make_plugin(apply))
    await fiber
    await dispose_fiber(fiber)

    assert order == ["c", "b", "a"], "LIFO ordering is relied on for teardown"


# ---------------------------------------------------------------------------
# 6. dispose() is awaitable and idempotent
# ---------------------------------------------------------------------------
async def test_dispose_is_awaitable_and_idempotent(root: Context) -> None:
    """``Fiber.dispose`` is assigned at runtime, returns a coroutine, and is safe twice."""
    fiber = root.plugin(make_plugin(lambda ctx, cfg: None))

    # It is not a declared method — the kernel assigns it per fiber.
    assert "dispose" not in vars(type(fiber))
    assert hasattr(fiber, "dispose")

    await dispose_fiber(fiber)
    assert fiber.get_state().name == "DISPOSED"

    await dispose_fiber(fiber)  # must not raise
    assert fiber.get_state().name == "DISPOSED"


# ---------------------------------------------------------------------------
# 7. The five dispatch modes
# ---------------------------------------------------------------------------
async def test_five_dispatch_modes(root: Context) -> None:
    """emit / parallel / serial / bail / waterfall each behave as documented."""
    hits: list[str] = []

    root.on("evt", lambda *a: hits.append("first") or "first")
    root.on("evt", lambda *a: hits.append("second") or "second")

    root.emit("evt")
    assert hits == ["first", "second"], "emit broadcasts to every listener"

    hits.clear()
    await root.parallel("evt")
    assert hits == ["first", "second"], "parallel runs every listener"

    assert await root.serial("evt") == "first", "serial returns the first non-null"
    assert root.bail("evt") == "first", "bail is the synchronous serial"


# ---------------------------------------------------------------------------
# 8/9. Waterfall conventions — next is LAST, and next(value) is ignored
# ---------------------------------------------------------------------------
def test_waterfall_next_is_the_last_positional(root: Context) -> None:
    """Listeners are called as ``listener(*pending, next)`` — next comes last."""
    captured: dict[str, object] = {}

    def listener(*args):
        *pending, nxt = args
        captured["pending"] = pending
        captured["next_callable"] = callable(nxt)
        return nxt()

    root.on("wf", listener)
    result = bind_waterfall(root, "wf", "PAYLOAD", terminal=lambda: "done")

    assert captured["pending"] == ["PAYLOAD"]
    assert captured["next_callable"] is True
    assert result == "done"


def test_waterfall_next_ignores_its_argument(root: Context) -> None:
    """``next(value)`` discards ``value``; the terminal is called with no args.

    This is a real deviation from the documented ``next(value)`` contract and it
    matters: the tool pipeline's ``PreDecision`` must travel via **return
    values**, not by being passed down through ``next``.
    """
    observed: dict[str, object] = {}

    def listener(*args):
        *_, nxt = args
        return nxt("THIS VALUE IS DISCARDED")

    def terminal(*args):
        observed["terminal_args"] = args
        return "terminal-value"

    root.on("wf", listener)
    result = bind_waterfall(root, "wf", terminal=terminal)

    assert observed["terminal_args"] == (), "terminal receives no arguments"
    assert result == "terminal-value", "return values flow backward up the chain"


def test_waterfall_without_next_short_circuits(root: Context) -> None:
    """A listener that never calls next() silently ends the chain.

    This is the documented Cordis footgun. We pin it as *expected* behaviour so
    the pipeline layer can defend against it rather than be surprised by it.
    """
    ran_second: list[bool] = []

    root.on("wf", lambda *args: "short-circuited")  # never calls next()
    root.on("wf", lambda *args: ran_second.append(True))

    result = bind_waterfall(root, "wf", terminal=lambda: "terminal")

    assert result == "short-circuited"
    assert ran_second == [], "later listeners are skipped"


def test_waterfall_double_next_raises(root: Context) -> None:
    """Calling next() twice in one listener is an error."""

    def bad_listener(*args):
        *_, nxt = args
        nxt()
        nxt()

    root.on("wf", bad_listener)

    with pytest.raises(RuntimeError, match="next\\(\\) called multiple times"):
        bind_waterfall(root, "wf", terminal=lambda: "terminal")


# ---------------------------------------------------------------------------
# 10/11. The service-read escape hatch
# ---------------------------------------------------------------------------
def test_root_context_read_is_unchecked(root: Context) -> None:
    """The ROOT context reads unchecked and returns None for missing names.

    The root fiber has ``runtime is None``, so ``handler_get`` takes a
    non-strict path. A typo against the root therefore yields ``None`` instead
    of raising. **Consequence for ForgePilot:** no helper object — above all
    ``ToolContext`` — may hold the root context, or it inherits this hole. This
    is the concrete reason the design forbids tools from constructing their own
    context.
    """
    assert root.fiber.runtime is None
    assert root.definitely_not_registered is None


def test_plugin_undeclared_read_of_missing_name_raises(root: Context) -> None:
    """Inside an activated plugin fiber, an unknown read raises rather than returning None.

    Contrast with the root context above: this is the path we get once a plugin
    declares what it needs, and it is why ``inject`` is load-bearing.
    """
    raised: list[str] = []

    def apply(ctx, config):  # noqa: ANN001, ARG001
        try:
            ctx.definitely_not_registered  # noqa: B018
        except AttributeError as exc:
            raised.append(str(exc))

    fiber = root.plugin(make_plugin(apply))

    assert fiber.get_state().name == "ACTIVE"
    assert raised and "without inject" in raised[0]


# ---------------------------------------------------------------------------
# 12. The provide signature we depend on
# ---------------------------------------------------------------------------
def test_provide_signature_is_name_value_check() -> None:
    """``Context.provide(name, value=None, check=None)`` — order confirmed from source.

    The ``check`` predicate is consulted by injectors to decide whether a
    service is *currently available*, which is how a request-scoped
    ``constraints`` service can be made to gate plugin activation.
    """
    params = list(inspect.signature(Context.provide).parameters)
    assert params[:4] == ["self", "name", "value", "check"]


# ---------------------------------------------------------------------------
# 13. The injection-declaration guard
# ---------------------------------------------------------------------------
def test_assert_declares_inject_guard() -> None:
    """The guard that stops a security-relevant dependency being read undeclared."""

    def plugin(ctx, config):  # noqa: ANN001, ARG001
        return None

    plugin.inject = ["tools"]

    assert plugin_declares(plugin) == {"tools"}
    assert plugin_declares({"inject": {"llm": {"model": "x"}}}) == {"llm"}

    assert_declares_inject(plugin, ["tools"])  # no raise

    with pytest.raises(PluginContractViolation, match="constraints"):
        assert_declares_inject(plugin, ["constraints"])
