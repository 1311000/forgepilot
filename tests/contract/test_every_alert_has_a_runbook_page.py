"""An alert with no page is a fact somebody has to invent a response to at 3 a.m.

Two files have to agree: `deploy/alerts.yml` says what fires, `docs/notes/runbook.md` says what
to do. Neither is generated from the other, so both directions drift unless something fails when
they do — this file is that something.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from forgepilot.cli.main import DEFAULT_SHUTDOWN_TIMEOUT_S
from forgepilot.httpapi.registry import DEFAULT_APPROVAL_TIMEOUT_S

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "deploy" / "alerts.yml"
RUNBOOK = ROOT / "docs" / "notes" / "runbook.md"

#: Every page answers these five questions, in this order and in this shape.
QUESTIONS = (
    "**What it means.**",
    "**First three commands.**",
    '**What "fixed" looks like.**',
    "**Who to wake.**",
    "**What it cannot tell you.**",
)

_DURATION = re.compile(r"^(\d+)([smhd])$")


def _alerts() -> dict[str, str]:
    """alert name -> its `for:` value, straight out of the rules file."""
    document = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for group in document.get("groups", []):
        for rule in group.get("rules", []):
            name = rule.get("alert")
            if name:
                found[name] = str(rule.get("for", ""))
    return found


def _pages() -> dict[str, str]:
    """alert name -> the markdown of its page, keyed by `### <Name>` headings."""
    text = RUNBOOK.read_text(encoding="utf-8")
    pages: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("### "):
            if current is not None:
                pages[current] = "\n".join(body)
            current = line[4:].strip()
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        pages[current] = "\n".join(body)
    return pages


def _seconds(value: str) -> int:
    match = _DURATION.match(value)
    assert match, f"{value!r} is not a duration this runbook knows (want e.g. 4m or 30s)"
    amount, unit = int(match.group(1)), match.group(2)
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def test_every_alert_has_a_page_and_every_page_names_an_alert() -> None:
    alerts, pages = set(_alerts()), set(_pages())
    assert not (alerts - pages), (
        "these alerts have no page in docs/notes/runbook.md: "
        f"{sorted(alerts - pages)}. An alert nobody wrote a response to is a page at 3 a.m."
    )
    assert not (pages - alerts), (
        "these pages name alerts deploy/alerts.yml does not contain: "
        f"{sorted(pages - alerts)}. A page for an alert that cannot fire is documentation of "
        "an intention, not of the system."
    )


@pytest.mark.parametrize("name", sorted(_alerts()))
def test_every_page_answers_the_five_questions(name: str) -> None:
    page = _pages()[name]
    missing = [question for question in QUESTIONS if question not in page]
    assert not missing, (
        f"{name}'s page does not answer {missing}. The shape is the point: a reader at 3 a.m. "
        "should find the same five answers in the same order on every page."
    )


def test_every_threshold_is_in_the_measurement_table() -> None:
    """A `for:` value that no table row explains is a number somebody guessed."""
    runbook = RUNBOOK.read_text(encoding="utf-8")
    rows = {
        line.split("|")[1].strip(): line
        for line in runbook.splitlines()
        if line.startswith("| ") and line.count("|") >= 3
    }
    for name, value in _alerts().items():
        row = rows.get(name)
        assert row is not None, (
            f"{name} has no row in the runbook's 'Where the thresholds come from' table, so "
            "nothing records where its threshold came from."
        )
        assert f"| {value} |" in row, (
            f"{name} is on `for: {value}` in alerts.yml but its runbook row says "
            f"{row.strip()!r} — the rule and the record of why disagree."
        )


def test_the_approval_alert_fires_before_the_approval_expires() -> None:
    """The defect this rule was written for: a 15-minute window over a five-minute timeout."""
    threshold = _seconds(_alerts()["ForgePilotApprovalWaiting"])
    assert threshold < DEFAULT_APPROVAL_TIMEOUT_S, (
        f"ForgePilotApprovalWaiting is on `for: {threshold}s` while an unanswered approval is a "
        f"refusal after {DEFAULT_APPROVAL_TIMEOUT_S}s: approvals_pending returns to 0 first, so "
        "the rule can never fire. It has to sit below the timeout it is meant to catch."
    )


def test_the_drain_alert_waits_for_the_shutdown_it_watches() -> None:
    threshold = _seconds(_alerts()["ForgePilotDraining"])
    assert threshold > DEFAULT_SHUTDOWN_TIMEOUT_S, (
        f"ForgePilotDraining is on `for: {threshold}s` while uvicorn is allowed "
        f"{DEFAULT_SHUTDOWN_TIMEOUT_S}s to finish in-flight connections: the alert would page "
        "during a normal drain."
    )
