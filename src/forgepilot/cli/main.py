"""ForgePilot CLI.

Commands available now::

    forgepilot --kernel-check                 boot the kernel, report fiber state
    forgepilot --dump-config                  print the resolved plugin tree
    forgepilot --profile web --dump-config    ...for a named profile
    forgepilot --config-dir configs --dump-config
    forgepilot --patch ./dev.yaml --dump-config

A real ``boot`` path (materialising the resolved tree into a live context) lands
with the provider packages at M2 — the seam rows compose today but do not yet
resolve to importable plugins.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from forgepilot import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forgepilot",
        description="ForgePilot — 企业级 Coding Agent 平台",
    )
    parser.add_argument("--version", action="version", version=f"forgepilot {__version__}")

    parser.add_argument(
        "--kernel-check",
        action="store_true",
        help="boot the Cordis kernel and report the root fiber state, then exit",
    )

    config = parser.add_argument_group("configuration")
    config.add_argument("--dump-config", action="store_true", help="print the resolved plugin tree")
    config.add_argument(
        "--boot",
        action="store_true",
        help="resolve the profile and actually load every enabled plugin",
    )
    config.add_argument("--profile", default="default", help="profile name (default: default)")
    config.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="directory containing profiles/ ; defaults to $FORGEPILOT_HOME or ~/.forgepilot",
    )
    config.add_argument(
        "--patch",
        type=Path,
        action="append",
        default=[],
        help="extra patch layer applied last; repeatable",
    )
    return parser


def _dump_config(args: argparse.Namespace) -> int:
    """Resolve the profile's layers and print the effective tree.

    Goes through exactly the same :func:`apply_layers` that a future boot path
    will use, so the printed tree cannot drift from what actually loads.
    """
    from forgepilot.config import ConfigError, apply_layers, compose_layers, load_profile

    try:
        profile = load_profile(args.config_dir, args.profile)
        layers = compose_layers(profile, args.patch)
        resolved = apply_layers(layers, profile=profile.name)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(resolved.dump(), end="")
    return 0


def _boot(args: argparse.Namespace) -> int:
    """Resolve the profile and load every enabled plugin for real.

    This and ``--dump-config`` consume the same :func:`apply_layers` result, so
    what the dump prints is what boots. That is the whole reason the two share a
    composition function.
    """
    from forgepilot.config import (
        ConfigError,
        apply_layers,
        apply_row,
        compose_layers,
        load_profile,
    )
    from forgepilot.kernel import create_root, fiber_state_name

    try:
        profile = load_profile(args.config_dir, args.profile)
        resolved = apply_layers(compose_layers(profile, args.patch), profile=profile.name)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    root = create_root()
    failures = 0

    print(f"profile: {profile.name}   mode: {profile.default_mode}")
    print(f"  {'row':<16} {'state':<10} plugin")

    for row in resolved.enabled_rows():
        try:
            fiber = apply_row(root, row)
            state = fiber_state_name(fiber) if fiber is not None else "skipped"
        except Exception as exc:  # noqa: BLE001 - report every row, do not stop at the first
            failures += 1
            print(f"  {row.id:<16} {'FAILED':<10} {row.plugin}")
            print(f"      {type(exc).__name__}: {exc}")
            continue
        print(f"  {row.id:<16} {state:<10} {row.plugin}")

    disabled = [row.id for row in resolved.rows if row.disabled]
    if disabled:
        print(f"\nnot loaded (disabled rows): {', '.join(disabled)}")

    print(f"\nresult: {'OK' if failures == 0 else str(failures) + ' row(s) FAILED'}")
    return 0 if failures == 0 else 1


def _kernel_check() -> int:
    """Boot the kernel through the ``kernel/`` package and report what we found.

    Deliberately goes through :mod:`forgepilot.kernel` rather than importing
    ``cordis_port`` here, so the kernel stays swappable behind one package.
    """
    from forgepilot.kernel import (
        apply_plugin,
        create_root,
        fiber_state_name,
        make_plugin,
    )

    root = create_root()
    loaded: list[str] = []

    def probe(ctx, config):  # noqa: ANN001, ARG001 — the kernel calls apply(ctx, config)
        loaded.append("apply ran")

    fiber = apply_plugin(root, make_plugin(probe))
    root_state = fiber_state_name(root.fiber)
    probe_state = fiber_state_name(fiber)

    print(f"forgepilot {__version__}")
    print("kernel        : cordis_port")
    print(f"root fiber    : {root_state}")
    print(f"probe fiber   : {probe_state}")
    print(f"apply invoked : {bool(loaded)}")

    ok = root_state == "ACTIVE" and probe_state == "ACTIVE" and bool(loaded)
    print(f"result        : {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.kernel_check:
        return _kernel_check()
    if args.dump_config:
        return _dump_config(args)
    if args.boot:
        return _boot(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
