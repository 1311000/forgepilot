"""The scrape endpoint: aggregate-only, bounded-cardinality, honest gauges.

Three properties are worth a test each, and they are the three ways a metrics
endpoint stops being usable rather than the three ways it could crash:

- **cardinality** — a label carrying a session id makes one series per session;
- **disclosure** — an unauthenticated endpoint must hold nothing that needs
  authentication to read;
- **freshness** — a gauge pushed from call sites drifts, one read at scrape time
  cannot.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="the `web` extra is not installed")


async def test_a_scrape_needs_no_credential_and_holds_nothing_personal(api) -> None:
    """The endpoint is unauthenticated *because* every number is a count.

    The session id is the check: it is known to the request and would be the
    obvious thing to put in a label, and its absence from the body is what
    "aggregate-only" means in practice.
    """
    created = await api.client.post("/sessions", headers=api.auth())
    assert created.status_code == 201, created.text
    session_id = created.json()["sessionId"]

    response = await api.client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = response.text
    assert "# TYPE forgepilot_http_requests_total counter" in body
    assert session_id not in body
    assert "alice" not in body
    assert "acme" not in body


async def test_a_request_is_counted_under_its_route_template(api) -> None:
    """``/sessions/{session_id}`` is one series; the concrete path is not."""
    created = await api.client.post("/sessions", headers=api.auth())
    session_id = created.json()["sessionId"]
    detail = await api.client.get(f"/sessions/{session_id}", headers=api.auth())
    assert detail.status_code == 200, detail.text

    body = (await api.client.get("/metrics")).text

    assert (
        'forgepilot_http_requests_total{method="GET",route="/sessions/{session_id}",'
        'status="200"} 1' in body
    )
    assert session_id not in body


async def test_an_unmatched_path_gets_one_label_not_a_series_per_url(api) -> None:
    """A scanner probing ``/.env`` must not create a new metric per URL."""
    first = await api.client.get("/nope-1")
    second = await api.client.get("/nope-2")
    assert first.status_code == 404
    assert second.status_code == 404

    body = (await api.client.get("/metrics")).text

    assert 'route="unmatched",status="404"} 2' in body
    assert "nope-1" not in body


async def test_the_session_gauge_is_read_from_the_registry_at_scrape_time(api) -> None:
    """The gauge falls when the registry does, without anyone updating it."""
    assert "forgepilot_sessions_live 0" in (await api.client.get("/metrics")).text

    created = await api.client.post("/sessions", headers=api.auth())
    assert created.status_code == 201
    assert "forgepilot_sessions_live 1" in (await api.client.get("/metrics")).text

    await api.registry.close()
    body = (await api.client.get("/metrics")).text
    assert "forgepilot_sessions_live 0" in body


async def test_the_ready_gauge_reads_the_same_flag_the_endpoint_does(api) -> None:
    """An alert that reads metrics must not disagree with a probe.

    `forgepilot_ready` exists so a "no ready replica" alert has a series to read;
    it is 1 until `registry.stop()` runs, which is exactly when `/readyz` starts
    returning 503. If the two were separate flags, this test would be the place
    that difference showed up.
    """
    assert "forgepilot_ready 1" in (await api.client.get("/metrics")).text
    assert (await api.client.get("/readyz")).status_code == 200

    api.registry.stop()

    assert "forgepilot_ready 0" in (await api.client.get("/metrics")).text
    assert (await api.client.get("/readyz")).status_code == 503


async def test_a_started_turn_is_counted_once(api) -> None:
    created = await api.client.post("/sessions", headers=api.auth())
    session_id = created.json()["sessionId"]

    started = await api.client.post(
        f"/sessions/{session_id}/messages", headers=api.auth(), json={"text": "hello"}
    )
    assert started.status_code == 202, started.text

    body = (await api.client.get("/metrics")).text
    assert "forgepilot_turns_started_total 1" in body


def test_a_broken_gauge_does_not_take_the_scrape_down() -> None:
    """One bad reader must not make the endpoint useless for the other nine.

    A scrape that returns 500 is a monitoring gap that looks like an outage; a
    scrape missing one series is a gap someone can see and fix.
    """
    from forgepilot.httpapi.metrics import Metrics

    metrics = Metrics()

    def boom() -> float:
        raise RuntimeError("the registry is being rebuilt")

    metrics.gauge("forgepilot_broken", "A gauge whose reader raises.", boom)
    metrics.gauge("forgepilot_fine", "A gauge that reads.", lambda: 7)

    body = metrics.render()

    assert "forgepilot_fine 7" in body
    assert "forgepilot_broken" not in body


def test_label_values_are_escaped() -> None:
    """A quote or a newline in a label must not forge a second series."""
    from forgepilot.httpapi.metrics import Metrics

    metrics = Metrics()
    metrics.observe_request("GET", '/odd"route\nhere', 200)

    body = metrics.render()

    assert '\\"route\\nhere' in body
    # Exactly one series: the escaped newline did not start a second line that
    # happens to look like a metric.
    assert sum(1 for line in body.splitlines() if line.startswith("forgepilot_http")) == 1


async def test_the_alert_rules_name_series_that_exist(api) -> None:
    """Every `forgepilot_*` series in `deploy/alerts.yml` is really emitted.

    A rule that names a series nobody exports never fires, and it looks exactly
    like monitoring until the day it is needed. The rules are checked against a
    live scrape rather than against a hand-written list, so adding a metric to the
    rules without adding it to `build_app` fails here.
    """
    from pathlib import Path

    import yaml

    rules_path = Path(__file__).resolve().parents[2] / "deploy" / "alerts.yml"
    document = yaml.safe_load(rules_path.read_text(encoding="utf-8"))

    # `forgepilot_turns_started_total` only exists after a turn has started, so the
    # scrape below is taken after one number of every kind has been produced.
    created = await api.client.post("/sessions", headers=api.auth())
    assert created.status_code == 201
    body = (await api.client.get("/metrics")).text
    emitted = {
        line.split("{")[0].split(" ")[0]
        for line in body.splitlines()
        if line.startswith("forgepilot_")
    }

    named: set[str] = set()
    for group in document["groups"]:
        for rule in group["rules"]:
            expression = rule["expr"]
            for token in expression.replace("(", " ").replace(")", " ").split():
                if token.startswith("forgepilot_"):
                    named.add(token)

    assert named, "the rules name no series at all — did the file move?"
    missing = sorted(named - emitted)
    assert missing == [], f"deploy/alerts.yml names series this build does not emit: {missing}"
