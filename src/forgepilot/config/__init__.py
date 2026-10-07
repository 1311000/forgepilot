"""Configuration plane: profile / bundle / patch composition.

Boot and ``--dump-config`` both go through :func:`apply_layers`. That single
composition function is the reason the two can never disagree about what is
loaded — do not add a second path that reads the config independently.
"""

from forgepilot.config.bundle import load_bundle_layer, load_patch_file, resolve_plugin
from forgepilot.config.dump import render
from forgepilot.config.errors import (
    BundleNotFound,
    ConfigError,
    DuplicateRowId,
    PatchFormatError,
    PluginResolutionError,
    ProfileNotFound,
)
from forgepilot.config.patch import (
    TIER_THIRD_PARTY,
    TIER_TRUSTED,
    PatchEntry,
    PatchLayer,
    ResolvedConfig,
    ResolvedRow,
    apply_layers,
    apply_row,
    expand_env,
    patch_layer_from_mapping,
)
from forgepilot.config.profile import (
    Profile,
    compose_layers,
    default_home,
    load_profile,
)

__all__ = [
    # composition
    "apply_layers",
    "apply_row",
    "patch_layer_from_mapping",
    "expand_env",
    "PatchEntry",
    "PatchLayer",
    "ResolvedConfig",
    "ResolvedRow",
    "TIER_TRUSTED",
    "TIER_THIRD_PARTY",
    # loading
    "load_profile",
    "compose_layers",
    "default_home",
    "Profile",
    "load_bundle_layer",
    "load_patch_file",
    "resolve_plugin",
    "render",
    # errors
    "ConfigError",
    "ProfileNotFound",
    "BundleNotFound",
    "PatchFormatError",
    "PluginResolutionError",
    "DuplicateRowId",
]
