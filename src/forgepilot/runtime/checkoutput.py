"""What a failed check says, reduced to the two things a repair loop needs.

A failing `pytest` run produces a traceback per failure and a summary at the end.
Both matter, and neither is free: the output is copied into the session log and
then into the next model request, so an unparsed multi-thousand-line run is paid
for on every subsequent turn of the task.

Two functions, deliberately separate:

- :func:`bound` keeps the **head and the tail** and says how much it dropped.
  Truncating only the tail is the tempting version and the wrong one — the tail
  is where the failing assertions and the summary live, which is exactly the part
  a repair loop reads.
- :func:`failed_tests` extracts test ids, so "3 failed" can be "3 failed:
  tests/test_paging.py::test_clamp, …" in a line the model cannot miss.

Neither guesses. A runner this file does not recognise yields no ids rather than
a wrong one: a made-up test name sends the agent to a file that does not exist,
which is worse than the blob it replaced.
"""

from __future__ import annotations

import re

__all__ = ["MAX_CHECK_OUTPUT_CHARS", "bound", "failed_tests"]

#: Head + tail budget for one check's output. Generous on purpose: a failing
#: mypy run on a large tree is long, and the point is to stop *unbounded* growth,
#: not to make the first failure unreadable.
MAX_CHECK_OUTPUT_CHARS = 20_000

#: How much of the budget the head gets. The tail keeps the rest, because the
#: summary and the last traceback are what a repair loop acts on.
_HEAD_SHARE = 0.6

#: pytest prints `FAILED tests/test_x.py::test_y - assertion text` in its short
#: summary, and `ERROR …` for collection errors.
_PYTEST = re.compile(r"^(?:FAILED|ERROR)\s+([^\s:]+(?:::[^\s]+)+)", re.MULTILINE)

#: unittest prints `FAIL: test_y (tests.test_x.TestX)`.
_UNITTEST = re.compile(r"^(?:FAIL|ERROR):\s+(\w+)\s+\(([\w.]+)\)", re.MULTILINE)

#: Nothing is gained by naming hundreds of ids; the first ones are the ones the
#: model will read.
MAX_FAILED_TESTS = 50


def bound(output: str, *, limit: int = MAX_CHECK_OUTPUT_CHARS) -> str:
    """Keep the head and the tail of ``output``, and say what was dropped."""
    if limit <= 0 or len(output) <= limit:
        return output
    head = int(limit * _HEAD_SHARE)
    tail = limit - head
    omitted = len(output) - limit
    marker = f"\n… [{omitted} characters omitted; the head and the tail are kept] …\n"
    return f"{output[:head]}{marker}{output[-tail:]}"


def failed_tests(output: str, *, limit: int = MAX_FAILED_TESTS) -> tuple[str, ...]:
    """Test ids named by the runner in ``output``, de-duplicated and stable.

    Recognises pytest's short summary and unittest's ``FAIL:``/``ERROR:`` lines.
    Anything else yields ``()`` — see the module docstring for why guessing is
    worse than the blob.
    """
    found: list[str] = []
    seen: set[str] = set()

    for match in _PYTEST.finditer(output):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            found.append(name)

    for match in _UNITTEST.finditer(output):
        test, where = match.groups()
        # unittest repeats the method inside the parentheses
        # (`test_clamp (tests.test_paging.TestClamp.test_clamp)`), and a doubled
        # id names nothing that can be run.
        owner = where[: -len(test) - 1] if where.endswith(f".{test}") else where
        name = f"{owner}.{test}" if owner else test
        if name not in seen:
            seen.add(name)
            found.append(name)

    return tuple(found[:limit])
