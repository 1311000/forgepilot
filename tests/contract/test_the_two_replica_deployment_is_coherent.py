"""The two-replica deployment files have to describe *the same* two replicas.

`deploy/compose.two.yaml` and `deploy/Caddyfile.two` are the deployment shape P33 measured
and the runbook describes. They are two files with one idea in them, so the drift this test
prevents is the ordinary kind: a third replica added to compose and not to the proxy (traffic
never reaches it), or a proxy that keeps sticky sessions (which would hide the one behaviour
worth seeing — a session readable from the replica that is not running it).

It parses text rather than running `docker compose config`, because the gate runs on machines
without a daemon; `docs/notes/deployment.md` records that the file was validated with the real
`docker compose config` when it was written.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "compose.two.yaml"
CADDY = ROOT / "deploy" / "Caddyfile.two"


def _services() -> dict[str, dict]:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document.get("services")
    assert isinstance(services, dict), "compose.two.yaml has no services"
    return services


def test_both_replicas_run_the_same_command_and_share_the_state() -> None:
    services = _services()
    replicas = [name for name in services if name.startswith("api-")]
    assert len(replicas) == 2, f"expected two API replicas, found {replicas}"

    commands = []
    for name in replicas:
        service = services[name]
        command = service.get("command")
        assert isinstance(command, list), f"{name} has no explicit command"
        assert "--multi-replica" in command, f"{name} does not run --multi-replica"
        assert any("coordination" in str(part) for part in command), (
            f"{name} does not load the coordination bundle, so the deployment has no lease"
        )
        volumes = [str(volume) for volume in service.get("volumes", [])]
        assert any(volume.startswith("state:") for volume in volumes), (
            f"{name} does not mount the shared state volume"
        )
        commands.append(tuple(command))
    assert commands[0] == commands[1], "the two replicas run different commands"


def test_the_proxy_names_every_replica_and_forwards_the_stream_unbuffered() -> None:
    services = _services()
    replicas = sorted(name for name in services if name.startswith("api-"))
    proxy = services.get("proxy")
    assert isinstance(proxy, dict), "compose.two.yaml has no proxy service"
    assert sorted(proxy.get("depends_on", [])) == replicas, (
        "the proxy does not depend on exactly the replicas that exist"
    )
    mounts = [str(volume) for volume in proxy.get("volumes", [])]
    assert any("Caddyfile.two" in mount for mount in mounts), (
        "the proxy uses the one-replica Caddyfile"
    )

    caddy = CADDY.read_text(encoding="utf-8")
    for name in replicas:
        assert f"{name}:8000" in caddy, f"Caddyfile.two does not forward to {name}"
    assert "flush_interval -1" in caddy, "Caddyfile.two buffers the SSE stream"
    assert "lb_policy round_robin" in caddy, "Caddyfile.two does not say how it balances"
    assert "sticky" not in caddy, "sticky sessions would hide what the second replica is for"


def test_the_one_replica_deployment_is_not_changed_by_this() -> None:
    single = yaml.safe_load((ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8"))
    replicas = [name for name in single["services"] if name.startswith("api-")]
    assert replicas == [], "compose.yaml grew replicas; the single-replica shape is a decision"
