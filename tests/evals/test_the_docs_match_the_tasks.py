"""The docs count the tasks, and nothing was counting the docs.

⚠️ **Found by asking "why 105?"** — I had told a person the sweep was 90 runs while
standing next to a set of seven tasks and five arms, because I added a task in that
same message and kept the count from the one before it. `run.py` had been printing
`[n/105]` the whole time. The number in the prose had nothing checking it.

Four places had the same stale count, all written when there were four tasks:

    evals/README.md:3    "Four tasks, five arms, one JSONL per run."
    evals/README.md:26   "~4 seconds for all four tasks"
    CLAUDE.md (was :195, now docs/notes/evals.md)
                         "`evals/` — four task shapes, five arms, ..."
    CLAUDE.md (was :819, now docs/notes/evals.md)
                         "Four tasks, five arms, one JSONL row per attempt."

The substantive half of that claim — **that every task is documented** — is
checkable, so it is checked here. A prose count will drift again; a missing table row
will fail.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import arms as arm_module  # noqa: E402
import harness as H  # noqa: E402

README = (FORGE / "evals" / "README.md").read_text(encoding="utf-8")
CLAUDE = (FORGE / "CLAUDE.md").read_text(encoding="utf-8")


def test_every_task_has_a_row_in_the_readme_table() -> None:
    """A task nobody documented is a task nobody can interpret when it fails."""
    for name in H.available_tasks():
        assert f"| `{name}` |" in README, (
            f"{name} exists in evals/tasks/ and has no row in evals/README.md — the "
            f"table is the only place a reader learns what it is for"
        )


def test_every_arm_has_a_row_in_the_readme_table() -> None:
    for name in arm_module.names():
        assert f"| `{name}` |" in README, f"arm {name} is undocumented"


def test_no_task_is_documented_that_does_not_exist() -> None:
    """The other direction, and the more damaging one: a table row for a task that
    was renamed or deleted sends a reader looking for a file that is not there."""
    documented = set(re.findall(r"^\| `([a-z0-9-]+)` \|", README, flags=re.MULTILINE))
    known = set(H.available_tasks()) | set(arm_module.names())
    # `full` and the task names are both in these tables; the risk markers and the
    # other tables in the file are filtered by requiring a real name.
    stray = {name for name in documented if "-" in name} - known
    assert not stray, f"documented but not present: {sorted(stray)}"


def test_the_readme_does_not_hand_count_the_task_set() -> None:
    """⚠️ **The count is the thing that drifted, four times.** A number in prose has
    nothing comparing it to the directory listing, and the sweep size is printed by
    `run.py` anyway — so the prose does not need to carry one.

    Deliberately narrow: it looks for the exact shape that went stale, not for
    digits in general, because "eight rule modules" in a task description is a fact
    about a fixture and is meant to be there.
    """
    stale = re.findall(r"\b(?:four|five|six|seven|eight)\s+(?:tasks|task shapes)\b", README)
    assert not stale, (
        f"the README hand-counts the task set: {stale}. Point at the table instead — "
        f"the count is {len(H.available_tasks())} today and was wrong in four files "
        f"the last time it changed"
    )


def test_the_front_door_points_at_the_authority_instead_of_restating_it() -> None:
    """⚠️ **`README.md` said「业务代码尚未开始」while a thousand tests passed.**

    Nothing read that file, so nothing could catch the drift — it was invisible until a person
    happened to open it, and it is the first thing a newcomer reads. The repair is structural
    rather than a fresher sentence: the front door no longer restates status at all. It names
    the one maintained list (`CLAUDE.md`'s State of the work) and the commands that re-derive
    the facts for themselves.

    Deliberately narrow, in the same spirit as the count guard above: this checks that the
    **pointer** is there, not what the section says. A test over prose would be either brittle
    or vacuous, and the failure it guards against is a second list, not a wrong adjective.
    """
    front = (FORGE / "README.md").read_text(encoding="utf-8")

    assert "State of the work" in front, (
        "README.md must point at CLAUDE.md's State of the work rather than restating it: it "
        "once claimed the business code had not started, a thousand passing tests later"
    )
