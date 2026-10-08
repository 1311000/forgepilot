"""ForgePilot CLI.

Commands available now::

    forgepilot --kernel-check                 boot the kernel, report fiber state
    forgepilot --dump-config                  print the resolved plugin tree
    forgepilot --config-dir configs --dump-config
    forgepilot --patch ./dev.yaml --dump-config
    forgepilot --boot                        resolve and load every enabled plugin
    forgepilot --chat                        interactive session against the model
    forgepilot --serve                       serve the HTTP/SSE API the UI talks to

``--dump-config`` never imports anything; ``--boot``, ``--chat`` and ``--serve``
do. That split is deliberate — a profile stays inspectable even when a plugin is
missing, broken, or not installed on this machine.
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

    chat = parser.add_argument_group("chat")
    chat.add_argument(
        "--chat",
        action="store_true",
        help="boot the profile and start an interactive session",
    )
    chat.add_argument("--token", default=None, help="credential; or set FORGEPILOT_TOKEN")
    chat.add_argument("--session", default=None, help="resume an existing session id")
    chat.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="directory the agent may read and write; defaults to the current directory",
    )
    chat.add_argument(
        "--allow-uncontained-exec",
        action="store_true",
        help=(
            "permit shell tools on a host whose sandbox is weaker than `full`. "
            "The default refuses them; see the warning this prints."
        ),
    )

    serve = parser.add_argument_group("serve")
    serve.add_argument(
        "--serve",
        action="store_true",
        help="boot the profile and serve the HTTP/SSE API the workbench talks to",
    )
    serve.add_argument("--host", default=None, help="bind address; overrides the bundle")
    serve.add_argument("--port", type=int, default=None, help="bind port; overrides the bundle")
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


def _serve(args: argparse.Namespace) -> int:
    """Boot the profile and serve the HTTP/SSE API.

    The same boot path as everything else, so what runs is what
    ``--dump-config`` prints — including which plugins are enabled, which is how
    a missing ``web`` bundle becomes a message rather than a 404 nobody can
    explain.
    """
    import asyncio

    from forgepilot.config import ConfigError, apply_layers, compose_layers, load_profile
    from forgepilot.kernel import create_root

    try:
        profile = load_profile(args.config_dir, args.profile)
        resolved = apply_layers(compose_layers(profile, args.patch), profile=profile.name)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Read the binding from the *resolved profile* rather than from the live
    # service. The kernel hands back a proxy whose methods are wrapped, and an
    # ASGI app must not travel out through one — see the note in
    # `httpapi/app.py`. Configuration is plain data, so it crosses fine.
    try:
        row = resolved.row("httpapi")
    except KeyError:
        row = None
    if row is None or row.disabled:
        print(
            "error: this profile enables no `httpapi` service, so there is nothing to "
            "serve. Add the `web` bundle, e.g. "
            "--patch src/forgepilot/bundles/web/patch.yaml",
            file=sys.stderr,
        )
        return 2

    # `web` is an optional extra on purpose — a headless deployment should not
    # have to install a web stack — so its absence is a normal state to be told
    # about clearly, not an ImportError traceback.
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print(
            'error: the HTTP surface needs the `web` extra: '
            'pip install -e ".[web]"',
            file=sys.stderr,
        )
        return 2

    from forgepilot.httpapi.app import build_app

    host = args.host or str(row.config.get("host", "127.0.0.1"))
    port = args.port or int(row.config.get("port", 8000))
    timeout = float(row.config.get("approval_timeout", 300.0))
    workspace = (args.workspace or Path.cwd()).resolve()

    async def run() -> int:
        import uvicorn

        root = create_root()
        try:
            await resolved.materialize_async(root)
        except Exception as exc:  # noqa: BLE001
            print(f"error: could not boot: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        # Built here, on a context held as a value: see `httpapi/app.py` for why
        # this is not a method on the plugin.
        app = build_app(root, workspace_root=workspace, approval_timeout=timeout)
        print(f"serving on http://{host}:{port}/   workspace {workspace}", file=sys.stderr)
        print("Ctrl-C to stop", file=sys.stderr)
        await uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="info")
        ).serve()
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


def _chat(args: argparse.Namespace) -> int:
    """Interactive chat against the profile's configured model.

    Goes through the same boot path as everything else, so what runs is what
    ``--dump-config`` prints.
    """
    import asyncio
    import os

    from forgepilot.config import ConfigError, apply_layers, compose_layers, load_profile
    from forgepilot.identity import ENV_KEY, ProjectPolicy, derive
    from forgepilot.kernel import create_root
    from forgepilot.runtime.assembly import assemble_session
    from forgepilot.seams.agent_loop import AgentSpec, UserInput
    from forgepilot.seams.refs import AgentId, SessionId
    from forgepilot.seams.session import SessionStore  # noqa: F401  (readability)

    try:
        profile = load_profile(args.config_dir, args.profile)
        resolved = apply_layers(compose_layers(profile, args.patch), profile=profile.name)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    token = args.token or os.environ.get("FORGEPILOT_TOKEN")
    if not token:
        print(
            "error: no credential. Pass --token or set FORGEPILOT_TOKEN.",
            file=sys.stderr,
        )
        return 2

    async def run() -> int:
        from forgepilot.seams.refs import Mode

        root = create_root()
        try:
            # The async form: we are inside a running loop, where the sync one
            # returns fibers that have not loaded and every lookup yields None.
            await resolved.materialize_async(root)
        except Exception as exc:  # noqa: BLE001
            print(f"error: could not boot: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        try:
            record = root.principal.resolve(token)
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}", file=sys.stderr)
            return 3

        constraints = derive(
            record.principal, record.grant, ProjectPolicy.open_project(), key=None
        )

        # Every tool's writes are bounded by this directory, and the sandbox
        # policy takes its root from here — so an unresolved path would widen the
        # boundary the sandbox enforces.
        workspace = (args.workspace or Path.cwd()).resolve()

        minimum_enforcement = None
        if args.allow_uncontained_exec:
            from forgepilot.seams.sandbox import SandboxEnforcement

            enforcement = root.sandbox.probe()
            if enforcement is not SandboxEnforcement.FULL:
                print(
                    f"⚠  allowing shell tools on a host that reports sandbox "
                    f"enforcement {enforcement.value!r}.\n"
                    f"   Commands will run WITHOUT real containment. Fine for a local "
                    f"fixture; do not do this where the agent can reach anything you "
                    f"care about.\n",
                    file=sys.stderr,
                )
            minimum_enforcement = SandboxEnforcement.NONE

        runtime = await assemble_session(
            root,
            constraints,
            session_id=SessionId(args.session) if args.session else None,
            workspace_root=workspace,
            tenant=str(record.principal.tenant_id),
            principal=str(record.principal.principal_id),
            minimum_enforcement_for_exec=minimum_enforcement,
        )

        spec = AgentSpec(
            agent_id=AgentId("cli"),
            session_id=runtime.session.header.session_id,
            mode=Mode(profile.default_mode),
            constraints=constraints,
            system_sections=("You are ForgePilot, an enterprise coding assistant.",),
        )
        loop = root.agents.build("forgepilot", spec, runtime.scope.ctx)
        agent = loop.create(spec)

        print(f"session {runtime.session.header.session_id}")
        print(f"model   {getattr(root.llm, 'model', '?')}   user {record.principal.principal_id}")
        print("type /exit to leave\n")

        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if line in {"/exit", "/quit"}:
                break
            if not line:
                continue
            result = await agent.run_turn(UserInput(text=line))
            print(f"\n{result.assistant_text}\n[{result.reason}, {result.tool_calls} tool call(s)]\n")

        await runtime.close()
        return 0

    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        if ENV_KEY not in os.environ:
            print(
                f"hint: {ENV_KEY} is unset, so authority cannot be signed.",
                file=sys.stderr,
            )
        return 1


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
    # Load a gitignored .env.local before anything reads the environment. The
    # real environment always wins, so an explicit `export` still takes
    # precedence over the file.
    from forgepilot.env import load_local_env

    load_local_env()

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.kernel_check:
        return _kernel_check()
    if args.dump_config:
        return _dump_config(args)
    if args.boot:
        return _boot(args)
    if args.chat:
        return _chat(args)
    if args.serve:
        return _serve(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
