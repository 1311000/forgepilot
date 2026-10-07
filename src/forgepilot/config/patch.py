"""The configuration plane: layered patch composition.

A running ForgePilot is a tree of plugins composed from ordered layers::

    bundles in declared order  ->  the user layer  ->  --patch overlays

**The central invariant is that boot and ``--dump-config`` share one function.**
Both call :func:`apply_layers`, which returns a :class:`ResolvedConfig`. Boot then
calls ``.materialize(ctx)``; inspection calls ``.dump()``. Because both consume
the *same resolved object*, the two can never disagree about what is loaded —
which is the whole point. Do not add a second code path that "just reads the
config".

Override semantics mirror DeepSeek Harness: an entry whose ``id`` already exists
**replaces that row's entire ``config``** rather than deep-merging into it. A
partial merge would make the effective value of a key depend on unrelated layers
and is genuinely hard to reason about; wholesale replacement keeps one row, one
owner.
"""

from __future__ import annotations

import inspect
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from forgepilot.config.errors import ConfigError, DuplicateRowId, PatchFormatError
from forgepilot.kernel.effects import make_plugin

__all__ = [
    "PatchEntry",
    "PatchLayer",
    "ResolvedRow",
    "ResolvedConfig",
    "apply_layers",
    "apply_row",
    "patch_layer_from_mapping",
    "expand_env",
]

#: ``${VAR}`` or ``${VAR:-default}`` inside a config value.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(
    value: Any,
    env: Mapping[str, str] | None = None,
    *,
    where: str = "config",
) -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` in config values, recursively.

    Without this, an unexpanded ``${VAR}`` would be dumped as a literal string —
    the configuration would *look* like it referenced the environment while
    actually passing the placeholder text through to the plugin. A missing
    variable with no default is an error rather than an empty string, for the
    same reason.
    """
    environ = os.environ if env is None else env

    if isinstance(value, str):

        def substitute(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in environ:
                return environ[name]
            if default is not None:
                return default
            raise ConfigError(
                f"{where}: environment variable {name!r} is not set and no default "
                f"was given; write ${{{name}:-<default>}} to supply one"
            )

        return _ENV_PATTERN.sub(substitute, value)

    if isinstance(value, Mapping):
        return {k: expand_env(v, environ, where=f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [expand_env(v, environ, where=f"{where}[{i}]") for i, v in enumerate(value)]
    return value

#: Trust tiers for plugins. Third-party plugins are denied the privileged phase
#: of the tool pipeline (see the design doc); the tier is declared here, is
#: visible in ``--dump-config``, and is asserted at boot.
TIER_TRUSTED = "trusted"
TIER_THIRD_PARTY = "third-party"
VALID_TIERS = frozenset({TIER_TRUSTED, TIER_THIRD_PARTY})


@dataclass(frozen=True, slots=True)
class PatchEntry:
    """One row contributed by one layer.

    ``tier`` and ``disabled`` are **three-state**: ``None`` means "I am not
    expressing an opinion", and an override inherits the previous value. This is
    not a nicety — treating "unspecified" as ``False``/``trusted`` would let a
    layer that only wanted to change ``config`` silently re-enable a disabled
    plugin, or silently promote a third-party plugin to trusted. Both are
    privilege changes nobody wrote down.
    """

    id: str
    plugin: str | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    inject: tuple[str, ...] | None = None
    tier: str | None = None
    disabled: bool | None = None


@dataclass(frozen=True, slots=True)
class PatchLayer:
    """An ordered set of rows from a single source.

    ``name`` is the provenance label that shows up in ``--dump-config``, e.g.
    ``"bundle:base"``, ``"user"``, ``"--patch:./dev.yaml"``.
    """

    name: str
    entries: tuple[PatchEntry, ...]


@dataclass(frozen=True, slots=True)
class ResolvedRow:
    """A row after all layers have been folded together.

    ``sources`` records every layer that touched this row, in application order,
    so the dump can answer "who set this?" without anyone reading YAML by hand.
    """

    id: str
    plugin: str | None
    config: Mapping[str, Any]
    inject: tuple[str, ...]
    tier: str
    disabled: bool
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """The effective configuration. Consumed identically by boot and by inspection."""

    rows: tuple[ResolvedRow, ...]
    layers: tuple[str, ...]
    profile: str = "default"

    # -- boot path ---------------------------------------------------------
    def materialize(
        self,
        ctx: Any,
        resolver: Callable[[str], Any] | None = None,
    ) -> Any:
        """Register every enabled row onto ``ctx``. Returns ``ctx``.

        ⚠️ **Synchronous only.** It works because the kernel spins a temporary
        event loop to completion when none is running. Inside a *running* loop it
        returns immediately with fibers that have not loaded — so a service
        lookup right afterwards yields ``None``. Use
        :meth:`materialize_async` from async code; the failure is silent, which
        is exactly why it is worth stating twice.

        ``resolver`` maps a ``"module:attr"`` spec to the object; tests inject a
        mapping-backed resolver so they need no real import machinery.
        """
        resolve = resolver or _default_resolver()
        for row in self.rows:
            apply_row(ctx, row, resolve)
        return ctx

    async def materialize_async(
        self,
        ctx: Any,
        resolver: Callable[[str], Any] | None = None,
    ) -> Any:
        """Register every row and **await each fiber**, so ``ctx.<name>`` works after.

        Awaiting is the only sound way to know a plugin has loaded: the fiber's
        state can read ACTIVE before its ``apply`` has run (see ``CLAUDE.md``,
        kernel note 7).
        """
        resolve = resolver or _default_resolver()
        for row in self.rows:
            fiber = apply_row(ctx, row, resolve)
            if fiber is not None:
                await fiber
        return ctx

    # -- inspection path ---------------------------------------------------
    def dump(self) -> str:
        """Render the resolved tree. Deterministic, byte-comparable."""
        from forgepilot.config.dump import render

        return render(self)

    def enabled_rows(self) -> tuple[ResolvedRow, ...]:
        return tuple(r for r in self.rows if not r.disabled)

    def row(self, row_id: str) -> ResolvedRow:
        for candidate in self.rows:
            if candidate.id == row_id:
                return candidate
        raise KeyError(row_id)


def _default_resolver() -> Callable[[str], Any]:
    from forgepilot.config.bundle import resolve_plugin

    return resolve_plugin


def apply_row(
    ctx: Any,
    row: ResolvedRow,
    resolver: Callable[[str], Any] | None = None,
) -> Any:
    """Register a single resolved row onto ``ctx``. Returns the fiber, or ``None``.

    Exposed separately from :meth:`ResolvedConfig.materialize` so a caller that
    wants per-row error reporting (the ``boot`` command) can get it without
    reimplementing how a row becomes a plugin.
    """
    if row.disabled or row.plugin is None:
        return None
    resolve = resolver or _default_resolver()
    target = resolve(row.plugin)
    return ctx.plugin(_as_plugin(target, row), dict(row.config))


def _as_plugin(target: Any, row: ResolvedRow) -> Any:
    """Adapt a resolved target into something ``ctx.plugin`` accepts.

    A class is registered as-is. A bare function must be wrapped by
    ``make_plugin``, which enforces the kernel's ``(ctx, config)`` arity — a
    one-argument ``apply`` otherwise registers silently and leaves the fiber
    FAILED.
    """
    if inspect.isclass(target):
        if row.inject:
            # Patch-level inject overrides what the class declared. Deliberate:
            # it is how a bundle swaps a plugin's dependency without forking it.
            setattr(target, "inject", list(row.inject))
        return target
    return make_plugin(target, inject=list(row.inject) if row.inject else None)


def apply_layers(
    layers: Sequence[PatchLayer],
    *,
    profile: str = "default",
) -> ResolvedConfig:
    """Fold ordered layers into the effective configuration.

    **This is the single composition function.** Boot and ``--dump-config`` must
    both go through it; there is deliberately no second path.
    """
    order: list[str] = []
    by_id: dict[str, ResolvedRow] = {}

    for layer in layers:
        seen_in_layer: set[str] = set()
        for entry in layer.entries:
            if entry.id in seen_in_layer:
                raise DuplicateRowId(
                    f"layer {layer.name!r} declares id {entry.id!r} more than once"
                )
            seen_in_layer.add(entry.id)

            if entry.tier is not None and entry.tier not in VALID_TIERS:
                raise PatchFormatError(
                    f"row {entry.id!r} in layer {layer.name!r} has unknown tier "
                    f"{entry.tier!r}; expected one of {sorted(VALID_TIERS)}"
                )

            previous = by_id.get(entry.id)
            if previous is None:
                order.append(entry.id)
                by_id[entry.id] = ResolvedRow(
                    id=entry.id,
                    plugin=entry.plugin,
                    config=dict(entry.config),
                    inject=tuple(entry.inject or ()),
                    tier=entry.tier or TIER_TRUSTED,
                    disabled=bool(entry.disabled),
                    sources=(layer.name,),
                )
                continue

            # Override: the whole config is replaced, not merged. `tier` and
            # `disabled` are inherited unless this layer states them, so an
            # override cannot change privilege by omission.
            by_id[entry.id] = replace(
                previous,
                plugin=entry.plugin if entry.plugin is not None else previous.plugin,
                config=dict(entry.config),
                inject=tuple(entry.inject) if entry.inject is not None else previous.inject,
                tier=entry.tier if entry.tier is not None else previous.tier,
                disabled=(
                    entry.disabled if entry.disabled is not None else previous.disabled
                ),
                sources=previous.sources + (layer.name,),
            )

    return ResolvedConfig(
        rows=tuple(by_id[row_id] for row_id in order),
        layers=tuple(layer.name for layer in layers),
        profile=profile,
    )


def patch_layer_from_mapping(name: str, raw: Any) -> PatchLayer:
    """Parse a layer from YAML-shaped data.

    Accepts either a bare list of rows or a mapping with an ``entries`` key::

        - id: llm
          plugin: forgepilot.providers.llm.anthropic:AnthropicLLM
          config: { model: claude-sonnet-5-5 }
    """
    if isinstance(raw, Mapping):
        if "entries" not in raw:
            raise PatchFormatError(f"layer {name!r}: mapping form requires an 'entries' key")
        raw = raw["entries"]

    if raw is None:
        return PatchLayer(name=name, entries=())

    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PatchFormatError(f"layer {name!r}: expected a list of rows, got {type(raw).__name__}")

    entries: list[PatchEntry] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise PatchFormatError(f"layer {name!r}: row {index} is not a mapping")
        if "id" not in item:
            raise PatchFormatError(f"layer {name!r}: row {index} has no 'id'")

        inject = item.get("inject")
        if isinstance(inject, Mapping):
            inject = tuple(inject.keys())
        elif isinstance(inject, str):
            inject = (inject,)
        elif inject is not None:
            inject = tuple(inject)

        entries.append(
            PatchEntry(
                id=str(item["id"]),
                plugin=item.get("plugin"),
                config=expand_env(
                    dict(item.get("config") or {}), where=f"{name}.{item['id']}.config"
                ),
                inject=inject,
                tier=str(item["tier"]) if "tier" in item else None,
                disabled=bool(item["disabled"]) if "disabled" in item else None,
            )
        )

    return PatchLayer(name=name, entries=tuple(entries))
