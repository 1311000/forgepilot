"""Bundle resolution: named plugin sets (in-tree) and plugin import specs."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

from forgepilot.config.errors import (
    BundleNotFound,
    ConfigError,
    PatchFormatError,
    PluginResolutionError,
)
from forgepilot.config.patch import PatchLayer, patch_layer_from_mapping

__all__ = ["bundles_dir", "resolve_plugin", "load_bundle_layer", "load_patch_file"]


#: Where the in-tree bundles live: ``src/forgepilot/bundles/<name>/patch.yaml``.
#: Third-party bundles are ordinary installable distributions and will resolve
#: through entry points once subprocess plugin isolation lands; they are out of
#: scope for M1.
def bundles_dir() -> Path:
    import forgepilot

    return Path(forgepilot.__file__).parent / "bundles"


def resolve_plugin(spec: str) -> Any:
    """Import ``"package.module:Attribute"`` and return the attribute.

    Deliberately explicit rather than a registry, so that ``--dump-config`` and a
    human reading the YAML see the same string.
    """
    if not isinstance(spec, str) or ":" not in spec:
        raise PluginResolutionError(
            f"plugin spec must look like 'package.module:Attribute', got {spec!r}"
        )
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise PluginResolutionError(f"malformed plugin spec {spec!r}")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise PluginResolutionError(f"cannot import {module_name!r} for {spec!r}: {exc}") from exc

    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise PluginResolutionError(f"{module_name!r} has no attribute {attr!r}") from exc


def load_patch_file(path: Path, *, name: str | None = None) -> PatchLayer:
    """Read a YAML patch layer from disk."""
    if not path.exists():
        raise ConfigError(f"patch file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PatchFormatError(f"{path} is not valid YAML: {exc}") from exc
    return patch_layer_from_mapping(name or path.name, raw)


def load_bundle_layer(name: str) -> PatchLayer:
    """Load an in-tree bundle's patch layer."""
    path = bundles_dir() / name / "patch.yaml"
    if not path.exists():
        available = sorted(p.name for p in bundles_dir().iterdir() if p.is_dir())
        raise BundleNotFound(f"no bundle {name!r} (looked in {path}); available: {available}")
    return load_patch_file(path, name=f"bundle:{name}")
