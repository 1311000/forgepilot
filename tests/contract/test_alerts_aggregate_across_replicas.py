"""The rules that ask "is there work" count replicas; the ones about one process do not.

A per-instance `forgepilot_turns_running > 0` fires on the replica that owns the
stuck turn and is silent about the idle one — which is correct per target and wrong
as a page, because a responder reading only the firing instance cannot tell whether
the fleet is busy or one process is wedged. The two work rules therefore aggregate;
`up` and `forgepilot_ready` deliberately do not, and this file pins both halves.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "deploy" / "alerts.yml"
DEPLOYMENT = ROOT / "docs" / "notes" / "deployment.md"


def _rules() -> dict[str, str]:
    parsed = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for group in parsed["groups"]:
        for rule in group["rules"]:
            found[rule["alert"]] = rule["expr"]
    return found


def test_the_work_rules_sum_across_replicas() -> None:
    rules = _rules()
    for name in ("ForgePilotTurnStall", "ForgePilotApprovalWaiting"):
        assert rules[name].startswith("sum("), (
            f"{name} is {rules[name]!r}: a per-replica count pages only the replica "
            f"that happens to hold the work"
        )


def test_the_per_process_rules_stay_per_target() -> None:
    rules = _rules()
    assert rules["ForgePilotDown"] == 'up{job="forgepilot"} == 0'
    assert rules["ForgePilotDraining"] == "max(forgepilot_ready) < 1"


def test_the_header_no_longer_promises_a_single_process() -> None:
    text = ALERTS.read_text(encoding="utf-8")
    assert "there is no second replica" not in text
    assert "each\n# replica answers for itself" in text, "the per-process boundary must be stated"


def test_the_deployment_note_names_what_is_aggregated() -> None:
    text = DEPLOYMENT.read_text(encoding="utf-8")
    assert re.search(r"already `sum\(\)` across\s+replicas", text), text[-2000:]
