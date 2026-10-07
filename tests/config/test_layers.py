"""M1 acceptance: the configuration plane.

The claim under test is not "layers merge" (that is easy) but **boot and
``--dump-config`` cannot disagree**, because both consume the output of the one
composition function. The test that actually holds that claim to account is
:func:`test_boot_registers_exactly_the_resolved_rows` — it materialises the
resolved config into a real context and asserts the plugins that ran are exactly
the enabled rows, in order, with the resolved config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgepilot.config import (
    DuplicateRowId,
    PatchFormatError,
    PatchLayer,
    apply_layers,
    compose_layers,
    load_profile,
    patch_layer_from_mapping,
    resolve_plugin,
)
from forgepilot.kernel import create_root

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_CONFIGS = REPO_ROOT / "configs"


def layer(name: str, *rows: dict) -> PatchLayer:
    return patch_layer_from_mapping(name, list(rows))


@pytest.fixture
def base() -> PatchLayer:
    return layer("bundle:base", {"id": "llm", "plugin": "pkg.llm:Base", "config": {"model": "base-1"}})


@pytest.fixture
def overlay() -> PatchLayer:
    return layer("user", {"id": "llm", "plugin": "pkg.llm:Over", "config": {"model": "over-1"}})


# ---------------------------------------------------------------------------
# Override behaviour and provenance
# ---------------------------------------------------------------------------
def test_overlay_wins_and_records_both_layers(base: PatchLayer, overlay: PatchLayer) -> None:
    cfg = apply_layers([base, overlay])

    row = cfg.row("llm")
    assert row.plugin == "pkg.llm:Over"
    assert row.config == {"model": "over-1"}
    assert row.sources == ("bundle:base", "user"), "--dump-config must name who set this"


def test_dropping_the_overlay_restores_the_base_value(base: PatchLayer) -> None:
    cfg = apply_layers([base])

    row = cfg.row("llm")
    assert row.plugin == "pkg.llm:Base"
    assert row.config == {"model": "base-1"}
    assert row.sources == ("bundle:base",)


def test_override_replaces_config_wholesale_rather_than_merging() -> None:
    """An override owns the whole row; there is no implicit deep merge.

    This matches the reference design and keeps "what is the effective value of
    key X" answerable from a single layer instead of a fold across several.
    """
    base = layer("bundle:base", {"id": "sandbox", "config": {"mode": "workspace-write", "network": "deny"}})
    overlay = layer("user", {"id": "sandbox", "config": {"mode": "read-only"}})

    row = apply_layers([base, overlay]).row("sandbox")

    assert row.config == {"mode": "read-only"}
    assert "network" not in row.config, "the base key is gone, not merged forward"


def test_row_order_follows_first_declaration() -> None:
    a = layer("a", {"id": "first"}, {"id": "second"})
    b = layer("b", {"id": "second"}, {"id": "third"})

    cfg = apply_layers([a, b])

    assert [r.id for r in cfg.rows] == ["first", "second", "third"]


def test_disabled_row_is_resolved_but_not_enabled() -> None:
    cfg = apply_layers([layer("a", {"id": "x", "disabled": True})])

    assert cfg.rows[0].disabled is True
    assert cfg.enabled_rows() == ()


# ---------------------------------------------------------------------------
# The invariant: one composition function feeding both paths
# ---------------------------------------------------------------------------
def test_boot_registers_exactly_the_resolved_rows(base: PatchLayer, overlay: PatchLayer) -> None:
    """Materialising the resolved config registers exactly the enabled rows.

    This is the honest form of "boot and dump agree": instead of comparing two
    dumps (which would be tautological), it drives the boot path into a live
    context and checks the result against the inspection path.
    """
    cfg = apply_layers([base, overlay])
    registered: list[tuple[str, dict]] = []

    def resolver(spec: str):
        def apply(_ctx, config):  # the kernel calls apply(ctx, config)
            registered.append((spec, dict(config)))

        return apply

    ctx = create_root()
    cfg.materialize(ctx, resolver=resolver)

    expected = [(r.plugin, dict(r.config)) for r in cfg.enabled_rows() if r.plugin]
    assert registered == expected


def test_dump_marks_disabled_rows_without_registering_them() -> None:
    cfg = apply_layers([layer("a", {"id": "on", "plugin": "p:X"}, {"id": "off", "plugin": "p:Y", "disabled": True})])
    registered: list[str] = []

    cfg.materialize(create_root(), resolver=lambda spec: (lambda _c, _cfg: registered.append(spec)))

    assert registered == ["p:X"]
    assert "disabled: true" in cfg.dump()


def test_dump_is_deterministic_and_byte_comparable(base: PatchLayer, overlay: PatchLayer) -> None:
    first = apply_layers([base, overlay]).dump()
    second = apply_layers([base, overlay]).dump()

    assert first == second
    assert first.endswith("\n")


# ---------------------------------------------------------------------------
# Malformed input is rejected loudly
# ---------------------------------------------------------------------------
def test_duplicate_id_within_one_layer_is_rejected() -> None:
    with pytest.raises(DuplicateRowId, match="more than once"):
        apply_layers([layer("a", {"id": "dup"}, {"id": "dup"})])


def test_unknown_tier_is_rejected() -> None:
    with pytest.raises(PatchFormatError, match="unknown tier"):
        apply_layers([layer("a", {"id": "x", "tier": "rootkit"})])


def test_row_without_id_is_rejected() -> None:
    with pytest.raises(PatchFormatError, match="no 'id'"):
        patch_layer_from_mapping("a", [{"plugin": "p:X"}])


def test_inject_may_be_a_mapping() -> None:
    parsed = patch_layer_from_mapping("a", [{"id": "x", "inject": {"tools": {}, "llm": {}}}])

    assert set(parsed.entries[0].inject or ()) == {"tools", "llm"}


# ---------------------------------------------------------------------------
# Environment expansion
#
# Without this an unexpanded ${VAR} would be dumped as a literal string: the
# config would look like it read the environment while actually passing the
# placeholder text to the plugin.
# ---------------------------------------------------------------------------
def test_env_var_is_expanded_in_config() -> None:
    from forgepilot.config import expand_env

    assert expand_env("${HOST}", {"HOST": "example.internal"}) == "example.internal"


def test_env_var_default_is_used_when_unset() -> None:
    from forgepilot.config import expand_env

    assert expand_env("${MISSING:-fallback}", {}) == "fallback"


def test_missing_env_var_without_default_is_an_error() -> None:
    from forgepilot.config import ConfigError, expand_env

    with pytest.raises(ConfigError, match="not set and no default"):
        expand_env("${MISSING}", {})


def test_env_expansion_reaches_nested_config() -> None:
    from forgepilot.config import expand_env

    expanded = expand_env(
        {"a": ["${V}", {"b": "${V}"}]},
        {"V": "x"},
    )

    assert expanded == {"a": ["x", {"b": "x"}]}


# ---------------------------------------------------------------------------
# Plugin resolution
# ---------------------------------------------------------------------------
def test_resolve_plugin_imports_a_real_attribute() -> None:
    fn = resolve_plugin("forgepilot.cli.main:_build_parser")

    assert callable(fn)


def test_resolve_plugin_rejects_a_bad_spec() -> None:
    from forgepilot.config import PluginResolutionError

    with pytest.raises(PluginResolutionError):
        resolve_plugin("no_colon_here")


# ---------------------------------------------------------------------------
# The shipped profile, end to end
# ---------------------------------------------------------------------------
def test_shipped_default_profile_composes_and_user_layer_wins() -> None:
    """The profile in ``configs/`` loads, and its user layer overrides the bundle."""
    profile = load_profile(SHIPPED_CONFIGS, "default")
    assert profile.bundles == ("base", "web")

    cfg = apply_layers(compose_layers(profile), profile=profile.name)

    llm = cfg.row("llm")
    assert llm.plugin == "forgepilot.providers.llm.anthropic:AnthropicLLMPlugin"
    assert llm.sources == ("bundle:base", "user")

    # The web bundle contributed its row, and base's rows survived.
    assert cfg.row("httpapi").sources == ("bundle:web",)
    assert cfg.row("sandbox").sources == ("bundle:base",)


def test_shipped_default_profile_dump_names_every_layer() -> None:
    profile = load_profile(SHIPPED_CONFIGS, "default")
    text = apply_layers(compose_layers(profile), profile=profile.name).dump()

    assert "profile: default" in text
    assert "bundle:base" in text
    assert "bundle:web" in text
    assert "user" in text


def test_extra_patch_layer_applied_last() -> None:
    profile = load_profile(SHIPPED_CONFIGS, "default")
    extra = SHIPPED_CONFIGS / "profiles" / "default" / "user.patch.yaml"

    layers = compose_layers(profile, [extra])
    cfg = apply_layers(layers, profile=profile.name)

    assert cfg.layers[-1] == f"--patch:{extra}"
    assert cfg.row("llm").sources == ("bundle:base", "user", f"--patch:{extra}")
