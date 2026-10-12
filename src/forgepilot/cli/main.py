"""ForgePilot CLI.

Commands available now::

    forgepilot --kernel-check                 boot the kernel, report fiber state
    forgepilot --dump-config                  print the resolved plugin tree
    forgepilot --config-dir configs --dump-config
    forgepilot --patch ./dev.yaml --dump-config
    forgepilot --boot                        resolve and load every enabled plugin
    forgepilot --sandbox-check               can this host contain what the profile needs?
    forgepilot --chat                        interactive session against the model
    forgepilot --task T-1                    run one ticket end to end
    forgepilot --distill <session-id> --repo host/ns/name
                                             turn one stored session into memories
    forgepilot --serve                       serve the HTTP/SSE API the UI talks to

``--dump-config`` never imports anything; ``--boot``, ``--chat``, ``--task`` and
``--serve`` do. That split is deliberate — a profile stays inspectable even when a
plugin is missing, broken, or not installed on this machine.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forgepilot import __version__
from forgepilot.console import use_utf8_output

if TYPE_CHECKING:  # pragma: no cover — annotations only, so `--dump-config` stays import-free
    from forgepilot.seams.refs import RepoRef


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
    config.add_argument(
        "--sandbox-check",
        action="store_true",
        help=(
            "ask the configured sandbox what it can enforce on this host, and exit "
            "non-zero when that is weaker than this profile requires"
        ),
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

    # Shared by every command that acts as someone.
    acting = parser.add_argument_group("acting as a caller")
    acting.add_argument("--token", default=None, help="credential; or set FORGEPILOT_TOKEN")
    acting.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="directory the agent may read and write; defaults to the current directory",
    )
    acting.add_argument(
        "--allow-uncontained-exec",
        action="store_true",
        help=(
            "permit shell tools on a host whose sandbox is weaker than `full`. "
            "The default refuses them; see the warning this prints."
        ),
    )

    chat = parser.add_argument_group("chat")
    chat.add_argument(
        "--chat",
        action="store_true",
        help="boot the profile and start an interactive session",
    )
    chat.add_argument("--session", default=None, help="resume an existing session id")

    task = parser.add_argument_group("task")
    task.add_argument(
        "--task",
        default=None,
        metavar="TICKET_ID",
        help="run the build/test/repair workflow for one ticket, end to end",
    )
    task.add_argument(
        "--project",
        type=Path,
        default=None,
        help=(
            "the project.yaml describing repos, checks and writable paths; "
            "defaults to the nearest one at or above the workspace"
        ),
    )

    memory = parser.add_argument_group("memory")
    memory.add_argument(
        "--distill",
        default=None,
        metavar="SESSION_ID",
        help=(
            "read one stored session log, extract memories from it, and exit; "
            "the value is the session id"
        ),
    )
    memory.add_argument(
        "--repo",
        default=None,
        metavar="HOST/NAMESPACE/NAME",
        help=(
            "the repository a distilled memory belongs to. Required by --distill: "
            "a memory's audience is a repository, and one with no audience cannot "
            "be recalled safely"
        ),
    )
    memory.add_argument(
        "--since",
        default=None,
        metavar="ISO8601",
        help="with --distill: only consider events at or after this timestamp",
    )
    memory.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "with --distill: really call the model and record the run, but write "
            "no memory and do not advance the watermark"
        ),
    )
    memory.add_argument(
        "--distill-token-cap",
        type=int,
        default=0,
        metavar="N",
        help=(
            "with --distill: stop without writing when one extraction exceeds N "
            "tokens. Checked after the call — a model call's cost is not knowable "
            "before it is made"
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
    from forgepilot.config import ConfigError

    try:
        _, resolved = _resolve(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(resolved.dump(), end="")
    return 0


def _resolve(args: argparse.Namespace) -> Any:
    """Resolve the profile into the effective tree, or raise :class:`ConfigError`.

    The one place the CLI turns arguments into a resolved configuration, so
    ``--dump-config``, ``--boot`` and ``--sandbox-check`` cannot disagree about
    which profile they are describing.
    """
    from forgepilot.config import apply_layers, compose_layers, load_profile

    profile = load_profile(args.config_dir, args.profile)
    return profile, apply_layers(compose_layers(profile, args.patch), profile=profile.name)


def _apply_rows(root: Any, resolved: Any) -> list[tuple[str, str, str, str | None]]:
    """Load every enabled row onto ``root``.

    Returns ``(row id, state, plugin, error)`` per row, where ``state`` is the
    fiber's state or ``"FAILED"`` with the exception in ``error``. One loader, so
    ``--boot``'s report and ``--sandbox-check``'s setup cannot drift about what
    loading a profile means.
    """
    from forgepilot.config import apply_row
    from forgepilot.kernel import fiber_state_name

    loaded: list[tuple[str, str, str, str | None]] = []
    for row in resolved.enabled_rows():
        try:
            fiber = apply_row(root, row)
            state = fiber_state_name(fiber) if fiber is not None else "skipped"
        except Exception as exc:  # noqa: BLE001 - report every row, do not stop at the first
            loaded.append((row.id, "FAILED", row.plugin, f"{type(exc).__name__}: {exc}"))
            continue
        loaded.append((row.id, state, row.plugin, None))
    return loaded


def _boot(args: argparse.Namespace) -> int:
    """Resolve the profile and load every enabled plugin for real.

    This and ``--dump-config`` consume the same :func:`apply_layers` result, so
    what the dump prints is what boots. That is the whole reason the two share a
    composition function.
    """
    from forgepilot.config import ConfigError
    from forgepilot.kernel import create_root

    try:
        profile, resolved = _resolve(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    root = create_root()
    failures = 0

    print(f"profile: {profile.name}   mode: {profile.default_mode}")
    print(f"  {'row':<16} {'state':<10} plugin")

    for row_id, state, plugin, error in _apply_rows(root, resolved):
        print(f"  {row_id:<16} {state:<10} {plugin}")
        if error is not None:
            failures += 1
            print(f"      {error}")

    disabled = [row.id for row in resolved.rows if row.disabled]
    if disabled:
        print(f"\nnot loaded (disabled rows): {', '.join(disabled)}")

    print(f"\nresult: {'OK' if failures == 0 else str(failures) + ' row(s) FAILED'}")
    return 0 if failures == 0 else 1


def _sandbox_row(resolved: Any, service: Any) -> Any:
    """The configuration row that provided ``root.sandbox``, or ``None``.

    Found by the **service name** the plugin registered, which is what a profile
    names its row after by convention (``id: sandbox``). ``None`` is a legitimate
    answer — a profile may call the row anything it likes — and the caller then
    prints the claim as undeclared rather than inventing one. A claim nobody can
    read is worth less than an honest "not declared"; the *verification* is
    ``probe()`` and does not depend on this.
    """
    name = getattr(service, "name", None)
    if not isinstance(name, str):
        return None
    try:
        return resolved.row(name)
    except KeyError:
        return None


def _sandbox_check(args: argparse.Namespace) -> int:
    """Ask the host the question ``arm`` asks before it hands out a shell: *can you contain this?*

    A :class:`~forgepilot.seams.sandbox.Sandbox` answers it with ``probe()``, and
    until this command nothing outside a live session ever called it. **``--boot``
    reports that the row loaded, which is a different claim**: the kernel-sandbox
    row loaded on this machine for as long as its boundary confined writes and not
    reads (finding 20, ``docs/notes/evals.md``), and nothing failed.

    Exit status is the point — a pipeline step can gate on it instead of reading
    prose. ``0`` when this host does what this profile requires, ``1`` when it does
    not, ``2`` when the configuration cannot be resolved or provides no sandbox.
    ``--allow-uncontained-exec`` lowers the requirement to ``none`` through the same
    helper ``--chat`` and ``--task`` use, so the flag keeps exactly one meaning.
    """
    import asyncio

    from forgepilot.config import ConfigError
    from forgepilot.kernel import create_root
    from forgepilot.runtime.tools.arm import enforcement_rank
    from forgepilot.seams.refs import SessionId
    from forgepilot.seams.sandbox import (
        SandboxEnforcement,
        SandboxExecutionPolicy,
        SandboxMode,
    )
    from forgepilot.seams.security import NetworkAccess

    try:
        profile, resolved = _resolve(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    root = create_root()
    broken = [
        (row_id, error)
        for row_id, _state, _plugin, error in _apply_rows(root, resolved)
        if error is not None
    ]

    sandbox = getattr(root, "sandbox", None)
    if sandbox is None or not hasattr(sandbox, "probe"):
        for row_id, error in broken:
            print(f"error: row {row_id!r} did not load: {error}", file=sys.stderr)
        print(
            "error: this profile provides no `sandbox` service, so there is no boundary "
            "to check. Put the sandbox row back (the base bundle carries one).",
            file=sys.stderr,
        )
        return 2

    # A row that failed *beside* the sandbox does not change the boundary, but it
    # does change what this profile can do — said out loud, without failing a
    # boundary that is genuinely fine.
    for row_id, error in broken:
        print(f"note: row {row_id!r} did not load: {error}", file=sys.stderr)

    row = _sandbox_row(resolved, sandbox)
    claim = (row.config.get("read_scope") if row is not None else None) or "not declared"
    service_name = getattr(sandbox, "name", "sandbox")
    workspace = (args.workspace or Path.cwd()).resolve()

    async def run_one() -> Any:
        policy = SandboxExecutionPolicy(
            mode=SandboxMode.WORKSPACE_WRITE,
            workspace_root=workspace,
            session_id=SessionId("sandbox-check"),
            network=NetworkAccess.DENY,
        )
        # The cheapest command there is, through the same seam a tool call uses.
        # `probe()` says what the backend believes; this says what happens.
        return await sandbox.run(["true"], policy=policy, cwd=workspace, env={}, timeout_s=30)

    try:
        actual = sandbox.probe()
        result = asyncio.run(run_one())
    except Exception as exc:  # noqa: BLE001 - a backend that cannot answer is the answer
        print(
            f"error: the `{service_name}` sandbox could not be exercised: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    required = _minimum_enforcement(root, args) or SandboxEnforcement.FULL

    print(f"profile      : {profile.name}")
    if row is not None:
        print(f"sandbox      : {row.id} -> {row.plugin}")
    else:
        print(f"sandbox      : service {service_name!r} (no row here is named that)")
    print(f"claim        : read_scope: {claim}")
    print(f"probe        : {actual.value}")
    print(f"required     : {required.value}")
    if result.executed:
        print(f"shell        : ran, exit code {result.exit_code}")
    else:
        print("shell        : refused, nothing executed")
        for line in (result.stderr or "").splitlines():
            print(f"               {line}")

    met = enforcement_rank(actual) >= enforcement_rank(required)
    ran = bool(result.executed) and result.exit_code == 0
    if met and (ran or required is SandboxEnforcement.NONE):
        print("result       : OK")
        return 0

    if not met:
        print(
            f"result       : FAILED — this profile requires {required.value!r} before a "
            f"tool may run a command, and this host's boundary reports {actual.value!r}"
        )
    else:
        print(
            f"result       : FAILED — the probe reports {actual.value!r}, but a command "
            f"could not be run through the boundary"
        )
    return 1


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
    _warn_if_the_binding_is_not_loopback(host, port)

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

        minimum_enforcement = _minimum_enforcement(root, args)

        runtime = await assemble_session(
            root,
            constraints,
            session_id=SessionId(args.session) if args.session else None,
            workspace_root=workspace,
            principal=record.principal,
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


def _minimum_enforcement(root: Any, args: argparse.Namespace) -> Any:
    """What ``--allow-uncontained-exec`` means, in one place.

    Both ``--chat`` and ``--task`` need it, and it has to mean the same thing in
    both. The flag does exactly one thing: lower the bar `arm` checks before it
    lets a command run. It does not change what the sandbox *reports* — a backend
    that says ``none`` goes on saying ``none`` — so the warning below is the only
    thing standing between an operator and the impression that they are contained.
    """
    if not args.allow_uncontained_exec:
        return None

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
    return SandboxEnforcement.NONE


def _warn_if_the_binding_is_not_loopback(host: str, port: int) -> None:
    """Say so when the API becomes reachable from somewhere other than this machine.

    The API authenticates with a bearer header over **plain HTTP**, and the
    credential it accepts may be a static entry from `FORGEPILOT_DEV_TOKENS`. On
    loopback that is a reasonable design — nothing else can reach it, so there is
    nothing to defend against; the workbench was even built to refuse a
    query-string token so the credential could not leak through a `Referer`. None
    of that reasoning survives `--host 0.0.0.0`, where the credential and the
    session log — which holds proprietary source — cross the network in clear text.

    ⚠️ **A warning rather than a refusal, deliberately, and for the same reason
    `--allow-uncontained-exec` warns.** A deployment that fronts this with a TLS
    proxy on a trusted network is a legitimate configuration, and a check that
    refused it would be this function inventing a policy nobody has decided. What
    is *not* acceptable is doing it silently: *a permissive default nobody is told
    about is how a demo configuration reaches production.*

    `localhost` and any loopback address are silent; a **name** that is not
    `localhost` is treated as exposed, because this cannot resolve it and guessing
    in the reassuring direction is the wrong way to be wrong.
    """
    from ipaddress import ip_address

    if host == "localhost":
        return
    try:
        if ip_address(host).is_loopback:
            return
    except ValueError:
        pass  # a hostname, or an empty bind: cannot prove it is loopback

    print(
        f"⚠  binding {host}:{port}, which is not loopback — this API is reachable "
        f"from other machines.\n"
        f"   It authenticates with a bearer token over plain HTTP, has no TLS, and "
        f"validates neither `Origin` nor `Host`: on an untrusted network the "
        f"credential and the session log (which contains proprietary source) are "
        f"readable in transit.\n"
        f"   Put it behind TLS on a trusted network, or bind 127.0.0.1.\n",
        file=sys.stderr,
    )


def _print_plan(plan: Any, gateway: Any) -> None:
    print(f"ticket    : {plan.ticket.id}  {plan.ticket.title}")
    print(f"plan      : {plan.state.value}  ({plan.reason})")
    if not plan.ready:
        return
    understanding = plan.understanding
    if understanding is not None:
        print(
            f"category  : {understanding.category}  "
            f"(confidence {understanding.confidence:.2f})"
        )
    assert plan.selection is not None  # noqa: S101 - invariant of `ready`
    print(f"tools     : {plan.selection.tool_set} -> {list(plan.selection.tools)}")
    print(f"repos     : {[r.slug for r in plan.repos]}")
    print(f"classifier calls: {gateway.calls}")


def _print_outcome(outcome: Any) -> None:
    print()
    print(f"state     : {outcome.state.value}   ({outcome.reason})")
    if outcome.branch:
        print(f"branch    : {outcome.branch}")
    if outcome.changed_paths:
        print(f"changed   : {list(outcome.changed_paths)}")
    for index, check in enumerate(outcome.checks, start=1):
        verdict = "did-not-run" if check.could_not_run else ("pass" if check.passed else "FAIL")
        print(f"check {index}   : {verdict}  exit={check.exit_code}")
        if check.output:
            for line in check.output.splitlines()[-8:]:
                print(f"             | {line}")
    if outcome.pull_request is not None:
        print(f"pull req  : {outcome.pull_request.url}")
    if outcome.draft is not None:
        print()
        print("--- the draft that would be raised ---")
        _print_capped(outcome.draft.body_md, 4000)


def _print_capped(text: str, limit: int) -> None:
    """Print, and say so when it is not everything.

    A silent cut reads as "that is the whole body", which is the same mistake as
    a search returning its first N hits without a marker.
    """
    if len(text) <= limit:
        print(text)
        return
    print(text[:limit])
    print(
        f"... [{len(text) - limit} more characters not shown. The full body is the "
        f"`task/state` event's `pullRequest` payload in the session log.]"
    )


def _parse_repo_slug(text: str | None) -> RepoRef | None:
    """`host/namespace/name` → :class:`RepoRef`, or None when it is not that.

    Host-qualified, like every other repository reference in this tree: the same
    `namespace/name` exists on two hosts, and a memory filed under the wrong one is
    a memory recalled by the wrong people.
    """
    from forgepilot.seams.refs import RepoRef

    parts = [part for part in str(text or "").split("/") if part]
    if len(parts) != 3:
        return None
    return RepoRef(host=parts[0], namespace=parts[1], name=parts[2])


def _print_distillation(report: Any, *, dry_run: bool) -> None:
    """Print what one distillation run did, in the shape a log line would have."""
    print(f"session       {report.session_id}")
    before, after = report.watermark_before, report.watermark_after
    print(f"watermark     {before} -> {after}{' (dry run: not advanced)' if dry_run else ''}")
    print(f"stretch       {report.events} events, {report.turns} closed turns")
    print(
        f"candidates    {report.candidates} extracted, {report.written} verified, "
        f"{report.provisional} provisional, {report.duplicated} duplicate, "
        f"{report.dropped} dropped"
    )
    if report.memories:
        print(f"memories      {', '.join(report.memories)}")
    print(
        f"tokens        {report.input_tokens} in / {report.output_tokens} out"
        f"   ${report.cost_usd:.4f}"
    )
    for note in report.notes:
        print(f"note          {note}")
    if report.failed:
        print(f"failed        {report.failed}: {report.detail}")


def _distill(args: argparse.Namespace) -> int:
    """Distil one stored session log into memories.

    **The one command that needs no identity.** Distilling writes memories, and a
    write is gated by the evidence a candidate cites rather than by a principal —
    the authority check happens when a memory is *recalled*, which is
    :func:`_chat`'s and :func:`_task`'s problem. That is also what makes this the
    command a scheduled job can run: it opens a log, and it cannot act on the
    host.
    """
    import asyncio
    from datetime import UTC, datetime

    from forgepilot.config import ConfigError, apply_layers, compose_layers, load_profile
    from forgepilot.kernel import create_root
    from forgepilot.runtime.distillation import distill
    from forgepilot.runtime.session import Session
    from forgepilot.seams.refs import SessionId
    from forgepilot.seams.security import Budget

    try:
        profile = load_profile(args.config_dir, args.profile)
        resolved = apply_layers(compose_layers(profile, args.patch), profile=profile.name)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    repo = _parse_repo_slug(args.repo)
    if repo is None:
        print(
            "error: --distill needs --repo host/namespace/name. A memory's audience "
            "is a repository; a memory with no audience cannot be kept out of "
            "another team's recall, so there is nothing safe to default it to.",
            file=sys.stderr,
        )
        return 2

    since = None
    if args.since:
        try:
            since = datetime.fromisoformat(str(args.since).replace("Z", "+00:00"))
        except ValueError as exc:
            print(f"error: --since is not an ISO-8601 timestamp: {exc}", file=sys.stderr)
            return 2
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)

    budget = Budget(max_tokens=args.distill_token_cap) if args.distill_token_cap else None
    session_id = SessionId(args.distill)

    async def run() -> int:
        root = create_root()
        try:
            await resolved.materialize_async(root)
        except Exception as exc:  # noqa: BLE001
            print(f"error: could not boot: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        try:
            session = await Session.open(root.sessionPersistence, session_id)
        except FileNotFoundError:
            print(
                f"error: no stored session {session_id!r}. A chat prints its id at "
                f"startup and `--dump-config` shows where sessions are kept.",
                file=sys.stderr,
            )
            return 4

        report = await distill(
            session,
            llm=root.llm,
            memory=root.memory,
            watermarks=root.distillationWatermarks,
            scope_ref=repo.slug,
            repo=repo,
            model=getattr(root.llm, "model", "") or "",
            budget=budget,
            since=since,
            dry_run=args.dry_run,
        )
        _print_distillation(report, dry_run=args.dry_run)
        return 0 if report.ok else 1

    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _task(args: argparse.Namespace) -> int:
    """Run one ticket end to end: plan, branch, work, check, repair, deliver.

    The same boot path as everything else, so what runs is what ``--dump-config``
    prints. In particular the **ticket comes through the ``tickets`` seam**
    rather than being built here: a CLI that fabricated its own ticket would keep
    working after a deployment swapped the tracker out, and would be reporting on
    a ticket nobody filed.
    """
    import asyncio
    import os

    from forgepilot.config import ConfigError, apply_layers, compose_layers, load_profile
    from forgepilot.config.project import find_project, load_project
    from forgepilot.identity import ENV_KEY, derive
    from forgepilot.kernel import create_root
    from forgepilot.orchestration import ClassifierGateway
    from forgepilot.orchestration.modes.task import plan_task, run_task
    from forgepilot.orchestration.task_state import TaskState
    from forgepilot.runtime.assembly import assemble_session, usage_tracker
    from forgepilot.runtime.loop.turn import DEFAULT_MAX_STEPS
    from forgepilot.seams.refs import TicketId

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

    workspace = (args.workspace or Path.cwd()).resolve()

    # Task mode cannot run without a project: it is where the repositories, the
    # checks and the writable set come from. Substituting the most restrictive
    # default would strip every permission at once, which reads as "the agent
    # refuses to do anything" rather than "the config was not found".
    try:
        project = load_project(args.project) if args.project else find_project(workspace)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if project is None:
        print(
            f"error: no project.yaml at or above {workspace}. Task mode needs one — "
            f"it names the repositories, the checks and what may be written. Pass "
            f"--project, or run from inside a project.",
            file=sys.stderr,
        )
        return 2

    async def run() -> int:
        root = create_root()
        try:
            await resolved.materialize_async(root)
        except Exception as exc:  # noqa: BLE001
            print(f"error: could not boot: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        try:
            record = root.principal.resolve(token)
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}", file=sys.stderr)
            return 3

        # The project contributes its own policy on top of the caller's grant.
        # It can only ever narrow — see `derive`.
        constraints = derive(record.principal, record.grant, project.to_policy(), key=None)

        try:
            ticket = await root.tickets.fetch(TicketId(args.task))
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 4

        runtime = await assemble_session(
            root,
            constraints,
            workspace_root=workspace,
            principal=record.principal,
            minimum_enforcement_for_exec=_minimum_enforcement(root, args),
        )
        try:
            gateway = ClassifierGateway(root.classifier)
            plan = await plan_task(
                ticket=ticket,
                project=project,
                constraints=constraints,
                registry=root.tools,
                classifier=gateway,
                workspace_root=workspace,
            )
            _print_plan(plan, gateway)
            if not plan.ready:
                print(
                    "\nthis ticket was not executed. Nothing was run, and no branch "
                    "was created.",
                    file=sys.stderr,
                )
                return 5

            outcome = await run_task(
                plan=plan,
                project=project,
                session=runtime.session,
                llm=root.llm,
                tools=root.tools,
                vcs=root.vcs,
                workspace=root.workspace,
                sandbox=root.sandbox,
                constraints=constraints,
                pipeline=runtime.pipeline,
                tracker=usage_tracker(runtime),
                model=getattr(root.llm, "model", "?"),
                # The outward-facing step is not a tool call, so it needs the
                # decision authority and the answerer directly.
                policy=root.policy,
                approval=root.approval,
                principal=record.principal,
                compactor=getattr(root, "compaction", None),
                # `getattr`, like compaction: a profile without a `memory` row is a
                # valid deployment, and a task with no memories is a task that runs
                # exactly as it did before this existed.
                memory=getattr(root, "memory", None),
                # Read from the `agentLoop` row rather than defaulted, so a
                # profile's `max_steps` bounds a *task* the way it bounds a chat.
                # It used not to reach here at all, and every task ran on 12.
                max_steps=getattr(getattr(root, "agentLoop", None), "max_steps", None)
                or DEFAULT_MAX_STEPS,
            )
            _print_outcome(outcome)
        finally:
            await runtime.close()

        # `READY` is a success: the checks passed, the work is committed, and the
        # only thing missing is a repo host. Exiting non-zero there would tell a
        # script that nothing happened, when in fact everything but the publish did.
        return 0 if outcome.state in (TaskState.DELIVERED, TaskState.READY) else 1

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

    use_utf8_output()
    load_local_env()

    parser = _build_parser()
    args = parser.parse_args(argv)

    # These are different commands, not flags that compose. Silently honouring the
    # first and dropping the rest is how `--task T-1 --chat` opens a chat prompt
    # and leaves someone wondering why the ticket never ran.
    chosen = [
        name
        for name, on in (
            ("--kernel-check", args.kernel_check),
            ("--dump-config", args.dump_config),
            ("--boot", args.boot),
            ("--sandbox-check", args.sandbox_check),
            ("--chat", args.chat),
            ("--task", bool(args.task)),
            ("--distill", bool(args.distill)),
            ("--serve", args.serve),
        )
        if on
    ]
    if len(chosen) > 1:
        parser.error(f"{' and '.join(chosen)} are separate commands; pass one.")

    if args.kernel_check:
        return _kernel_check()
    if args.dump_config:
        return _dump_config(args)
    if args.boot:
        return _boot(args)
    if args.sandbox_check:
        return _sandbox_check(args)
    if args.chat:
        return _chat(args)
    if args.task:
        return _task(args)
    if args.distill:
        return _distill(args)
    if args.serve:
        return _serve(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
