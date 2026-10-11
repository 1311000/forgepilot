"""Rendering the resolved configuration.

Deterministic by construction: rows appear in composition order, and each row's
``config`` is serialised with sorted keys. That makes the output byte-comparable,
which is what lets a test assert that boot and inspection agree.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from forgepilot.config.patch import ResolvedConfig

__all__ = ["render"]


def render(cfg: ResolvedConfig) -> str:
    """Render ``cfg`` as a stable, greppable text tree.

    Every row carries its ``sources``, so "which layer set the sandbox mode to
    workspace-write" is answerable without reading YAML by hand.
    """
    lines: list[str] = []
    lines.append(f"profile: {cfg.profile}")

    layers = list(cfg.layers)
    if layers:
        lines.append("layers:")
        lines.extend(f"  - {layer}" for layer in layers)
    else:
        lines.append("layers: []")

    enabled = sum(1 for row in cfg.rows if not row.disabled)
    lines.append(f"rows: {len(cfg.rows)} ({enabled} enabled)")
    lines.append("")

    for row in cfg.rows:
        lines.append(f"- id: {row.id}")
        lines.append(f"  plugin: {row.plugin if row.plugin is not None else '<no plugin>'}")
        lines.append(f"  tier: {row.tier}")
        lines.append(f"  disabled: {'true' if row.disabled else 'false'}")
        lines.append(f"  inject: {list(row.inject)}")
        config_json = json.dumps(dict(row.config), sort_keys=True, ensure_ascii=False)
        lines.append(f"  config: {config_json}")
        lines.append(f"  sources: {list(row.sources)}")

    return "\n".join(lines) + "\n"
