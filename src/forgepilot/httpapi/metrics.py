"""A handful of numbers, in the exposition format Prometheus already reads.

Why hand-rendered instead of ``prometheus_client``: this needs four counters, four
gauges and no histogram, and the dependency would be the largest thing in the
image for the sake of a text format that is thirty lines to emit. The format
choice is not a shortcut, though — ``text/plain; version=0.0.4`` is a published
specification and a scraper does not care who produced it.

Two rules the shape enforces:

- **Route labels are templates, never paths.** ``/sessions/{session_id}`` is
  bounded; ``/sessions/01J…`` is one series per session, which is how a metrics
  endpoint becomes the thing that takes the monitoring system down.
- **Aggregates only.** No tenant, no principal, no session id, no ticket number.
  A metric is a place people look without the authorisation the data itself
  requires, and a count that can be read out of context is still a disclosure.

Latency histograms and per-turn token counts are deliberately absent: a histogram
needs buckets that should be chosen from measured traffic, and the token counts
already live in the session log, which is the record this platform treats as
authoritative. A second copy in a metric is a second thing to keep in sync.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

__all__ = ["CONTENT_TYPE", "Metrics"]

#: The Prometheus text format's media type, version pinned. A scraper negotiates
#: on it; ``text/plain`` alone gets parsed as the oldest format some servers emit.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape(value: str) -> str:
    """Escape a label value per the exposition format."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    """Counters and gauges for one assembled app.

    Not shared between apps, and not global: two apps in one process (a test can
    build two) must not report each other's traffic, and a module-level registry
    would also make every test's numbers depend on test order.
    """

    def __init__(self) -> None:
        self._requests: Counter[tuple[str, str, int]] = Counter()
        self._counters: Counter[str] = Counter()
        self._counter_help: dict[str, str] = {}
        self._gauges: list[tuple[str, str, Callable[[], float]]] = []

    # -- writing -----------------------------------------------------------
    def observe_request(self, method: str, route: str, status: int) -> None:
        self._requests[(method.upper(), route, status)] += 1

    def counter(self, name: str, help_text: str, amount: int = 1) -> None:
        """Add to a named counter. The help text is set on first use."""
        self._counter_help.setdefault(name, help_text)
        self._counters[name] += amount

    def gauge(self, name: str, help_text: str, read: Callable[[], float]) -> None:
        """Register a gauge whose value is *read at scrape time*.

        A pushed gauge has to be updated at every site that could change it, and
        the site that gets forgotten reports a number that is wrong in the
        reassuring direction — a live-session count that stops falling. Reading
        it from the object that owns the truth cannot drift from it.
        """
        self._gauges.append((name, help_text, read))

    # -- reading -----------------------------------------------------------
    def render(self) -> str:
        lines: list[str] = []

        grouped: dict[tuple[str, str], Counter[int]] = {}
        for (method, route, status), count in self._requests.items():
            grouped.setdefault((method, route), Counter())[status] += count
        if grouped:
            lines.append("# HELP forgepilot_http_requests_total Requests served, by route.")
            lines.append("# TYPE forgepilot_http_requests_total counter")
            for (method, route), by_status in sorted(grouped.items()):
                for status, count in sorted(by_status.items()):
                    lines.append(
                        "forgepilot_http_requests_total"
                        f'{{method="{_escape(method)}",route="{_escape(route)}",'
                        f'status="{status}"}} {count}'
                    )

        for name in sorted(self._counters):
            help_text = self._counter_help.get(name, name)
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {self._counters[name]}")

        for name, help_text, read in self._gauges:
            try:
                value: Any = read()
            except Exception:  # noqa: BLE001 - a broken gauge must not fail the scrape
                continue
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")

        # The format wants a trailing newline; some scrapers reject a body that
        # does not end with one, and ``join`` of an empty list is ``""``.
        return "\n".join(lines) + "\n"
