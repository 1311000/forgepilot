"""Profile loading and layer ordering.

Layout::

    $FORGEPILOT_HOME/
      profiles/<name>/
        profile.yaml        manifest: name, ordered bundle list, default_mode
        user.patch.yaml     the user layer (optional)
        plugins/            out-of-tree plugins (later; needs subprocess isolation)

Composition order is fixed and deliberate: **bundles in declared order, then the
user layer, then any ``--patch`` overlays.** Later wins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml

from forgepilot.config.bundle import load_bundle_layer, load_patch_file
from forgepilot.config.errors import ConfigError, ProfileNotFound
from forgepilot.config.patch import PatchLayer

__all__ = ["Profile", "default_home", "load_profile", "compose_layers"]

USER_LAYER_FILENAME = "user.patch.yaml"


def default_home() -> Path:
    """``$FORGEPILOT_HOME`` if set, else ``~/.forgepilot``."""
    env = os.environ.get("FORGEPILOT_HOME")
    return Path(env) if env else Path.home() / ".forgepilot"


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    bundles: tuple[str, ...]
    default_mode: str
    root: Path


def load_profile(home: Path | None = None, name: str = "default") -> Profile:
    """Read ``profiles/<name>/profile.yaml``."""
    base = home if home is not None else default_home()
    root = base / "profiles" / name
    manifest = root / "profile.yaml"
    if not manifest.exists():
        raise ProfileNotFound(f"no profile {name!r} at {manifest}")

    try:
        data: Any = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{manifest} is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{manifest} must contain a mapping")

    bundles = data.get("bundles") or []
    if not isinstance(bundles, Sequence) or isinstance(bundles, (str, bytes)):
        raise ConfigError(f"{manifest}: 'bundles' must be a list")

    return Profile(
        name=str(data.get("name", name)),
        bundles=tuple(str(b) for b in bundles),
        default_mode=str(data.get("default_mode", "chat")),
        root=root,
    )


def compose_layers(
    profile: Profile,
    extra_patches: Sequence[Path] = (),
) -> tuple[PatchLayer, ...]:
    """Build the ordered layer list for ``profile``.

    Order: bundles (declared order) -> user layer -> ``--patch`` overlays.
    """
    layers: list[PatchLayer] = [load_bundle_layer(name) for name in profile.bundles]

    user_layer = profile.root / USER_LAYER_FILENAME
    if user_layer.exists():
        layers.append(load_patch_file(user_layer, name="user"))

    for patch in extra_patches:
        layers.append(load_patch_file(Path(patch), name=f"--patch:{patch}"))

    return tuple(layers)
