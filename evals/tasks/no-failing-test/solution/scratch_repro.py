"""The scratch file the first live run of this task wrote, kept as evidence.

⚠️ **This is neither a solution nor a cheat.** It is a reproduction of an observed run,
retained because it is the cleanest thing the task set has for a clause the task was
missing. The agent spent 17 tool calls, wrote this, and stopped — its last line of text
was "Let me run from there:". `percentiles.py` was never touched.

The visible checks then passed, because they pass on the unmodified tree; the runner
recorded `ready` with `checks: pass`; and the row was caught only by the held-out probe.
That is why `task.yaml` now also declares `changed_paths_include: [percentiles.py]`, and
why this file exists as a script input: the failure is a regression test rather than a
line in a findings list.

**Script input, never shipped code.**
"""

from __future__ import annotations

from percentiles import p50
from reporting import percentile_rank

VALUES = [1.0, 2.0, 3.0, 4.0]

print("dashboard:", p50(VALUES))
print("report   :", percentile_rank(VALUES, 50))
