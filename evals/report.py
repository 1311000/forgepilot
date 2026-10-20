"""Summarise eval results.

    python evals/report.py                     # every result file
    python evals/report.py --latest            # just the newest run

Reads the JSONL the runner appends and prints two tables: per (task, arm), and
per arm. **Nothing here is a verdict on its own.** A single row says almost
nothing about a stochastic system, and the report says so where it prints rather
than leaving the reader to remember it.

⚠️ **Scripted rows measure the harness, not the agent.** A replay model performs
a fix because it was told to; the rate it produces is a statement about the
machine. They are included because a broken harness must be visible, and labelled
so nobody reads them as a delivery rate.
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parent / "results"


def load(pattern: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(glob.glob(str(RESULTS / pattern))):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _median(values: list[float]) -> str:
    return f"{statistics.median(values):.0f}" if values else "-"


def _pass_at(row: dict[str, Any], attempts: int) -> bool | None:
    """Whether the check passed **on or before** attempt ``attempts``.

    **Cumulative on purpose**: that is what makes the two numbers comparable, and
    what Aider's leaderboard means by them. A run that passed on the first attempt
    also passed within two, so ``pass@2 >= pass@1`` always, and the gap between the
    two columns *is* the work the repair loop did — the only number that can justify
    the loop's existence.

    Three states, deliberately distinct:

    - ``True`` / ``False`` from a recorded list — an attempt was made and that is
      what it decided (an empty list is "attempts were made and none passed");
    - ``None`` when the row predates the recording. **A missing number, not a
      zero**: an old file has no opinion about whether the first attempt passed,
      and counting it as a failure would invent a result.
    """
    verdicts = row.get("check_verdicts")
    if verdicts is None:
        return None
    return any(verdicts[:attempts])


def _attempt_columns(group: list[dict[str, Any]]) -> tuple[str, str, bool]:
    """``(pass@1, pass@2, every row recorded)`` for one cell."""
    ones = [_pass_at(row, 1) for row in group]
    twos = [_pass_at(row, 2) for row in group]
    if any(value is None for value in ones) or any(value is None for value in twos):
        return "-", "-", False
    return f"{sum(ones)}/{len(group)}", f"{sum(twos)}/{len(group)}", True


def _delivery_column(group: list[dict[str, Any]]) -> str:
    """How many repositories a row delivered to, as a median — or ``-``.

    M13 made one ticket able to cover several repositories, each delivered on its own
    (one worktree, one commit, one pull request each), so "the task passed" no longer
    implies "every repository it named was delivered". This column is the size of the
    unit those rows were scored on.

    ⚠️ **Absent is not zero, and that is why the key is looked for rather than
    `get`-ed with a default.** A row written before M13 has no `deliveries` key at all;
    a row that recorded an empty tuple is a task that planned no repository. Printing
    `0` for the first would read as "this task delivered nothing", which is the
    opposite of what those rows show — the same distinction `_pass_at` draws.
    """
    recorded = [len(r["deliveries"]) for r in group if "deliveries" in r]
    return _median([float(count) for count in recorded])


def _print_grouped(rows: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["task"], row["arm"])].append(row)

    print(
        f"{'task':<14}{'arm':<14}{'mode':<10}{'n':>3}{'pass':>7}"
        f"{'pass@1':>8}{'pass@2':>8}{'calls':>7}{'turns':>7}{'secs':>7}{'deliv':>7}  states"
    )
    print("-" * 111)
    for (task, arm), group in sorted(groups.items()):
        passed = sum(1 for r in group if r.get("passed"))
        first, second, _ = _attempt_columns(group)
        states = ",".join(sorted({str(r.get("state", "-")) for r in group}))
        print(
            f"{task:<14}{arm:<14}{group[0].get('mode', '-'):<10}"
            f"{len(group):>3}{passed:>4}/{len(group):<2}"
            f"{first:>8}{second:>8}"
            f"{_median([r.get('tool_calls', 0) for r in group]):>7}"
            f"{_median([r.get('turns', 0) for r in group]):>7}"
            f"{_median([r.get('wall_s', 0) for r in group]):>7}"
            f"{_delivery_column(group):>7}  {states}"
        )

    if groups and not all(_attempt_columns(g)[2] for g in groups.values()):
        print(
            "\n⚠️  pass@1 / pass@2 read `-`: this file was written before the attempts\n"
            "   were recorded per row. An old row is not a row that failed its first\n"
            "   attempt — re-run to compare them."
        )


def _print_attempt_gap(rows: list[dict[str, Any]]) -> None:
    """The repair loop's own number: how many runs it turned around.

    Printed apart from the tables because it is a statement about a *mechanism*
    rather than about a cell, and because it is the one figure an ablation of the
    loop (`no-repair`) is supposed to move.
    """
    recorded = [r for r in rows if _pass_at(r, 1) is not None]
    if not recorded:
        return

    first = sum(1 for r in recorded if _pass_at(r, 1))
    within_two = sum(1 for r in recorded if _pass_at(r, 2))
    final = sum(1 for r in recorded if r.get("passed"))
    print(
        f"\nThe repair loop, on {len(recorded)} row(s) that recorded their attempts:\n"
        f"  passed on the first attempt : {first}/{len(recorded)}\n"
        f"  passed within two attempts  : {within_two}/{len(recorded)}\n"
        f"  passed the row's own test   : {final}/{len(recorded)}"
    )
    if within_two > first:
        print(
            f"  -> the second attempt converted {within_two - first} run(s) that the\n"
            f"     first did not. Without this split that work is invisible: both\n"
            f"     columns would have been the one `pass` rate."
        )
    else:
        print(
            "  -> the second attempt converted nothing here. That is a result too: on\n"
            "     these tasks the loop is not earning its place (or nothing failed first)."
        )


def _routed_away(row: dict[str, Any]) -> bool:
    """A row the **router** ended before any work happened, on a task that did not want it.

    ⚠️ **Live routing is a model decision, and a model can decline.** `harness.boot` swaps in
    the fixed classifier only under `--scripted`, so a live run's ticket is classified by the
    real model — the `understanding:` block in a task file is a replay fixture, not what live
    uses. When it declines, the run ends `awaiting_info` with no turns and no tool calls, and
    the row fails however good the agent would have been. Counting that in an arm's pass rate
    measures the router.

    ⚠️ **`awaiting_info` alone is not the signature**, and testing only the state would hide
    real results in the other direction: `incomplete` *expects* that state, and its rows pass.
    Three conditions together are the signature — the state, no work done, and a row the task
    did not accept. Measured rate when this was written: 1 of 138 live rows.
    """
    return (
        row.get("state") == "awaiting_info"
        and not row.get("passed")
        and not row.get("turns")
        and not row.get("tool_calls")
    )


def _escaped(row: dict[str, Any]) -> bool:
    """A row whose agent ran shell commands outside the checkout **and got something**.

    ⚠️ **`score` already says what such a row is worth**, in its own words: *"so nothing in this
    row is evidence about the agent"*. Counting it as an agent failure therefore measures the
    opposite of what the row shows — the row is evidence that the sandbox was **probed**, which is
    worth reading on its own and worth not blending into a pass rate.

    Found on `not-in-the-checkout`, the task whose answer is not on disk: with the retriever
    removed the agent cannot reach the contract from its checkout, so it went looking on the host —
    `dir /s /b …\\.forgepilot | findstr /i "contract index"`, and once into the session logs.
    **Three of three `no-search` rows did it**, which is the behaviour, not a coincidental reason
    string.

    ⚠️ **Since finding 21 this is the *effect*-aware key, not every attempt.** With the boundary
    confining reads the same three rows still tried (`ls -la /eval`, `nslookup git.local`,
    `git clone http://git.local/…`) and were refused, and this function, reading the attempt,
    marked all three aside — which is the opposite of what they show: a command that was refused
    is evidence the agent *stayed* inside the boundary. `escaped_workspace` is computed from each
    flagged call's paired `tool/result`; the attempt-only key is still read for the rows recorded
    before it existed.
    """
    if "escaped_workspace" in row:
        return bool(row.get("escaped_workspace"))
    return bool(row.get("left_the_workspace"))


def _aside(row: dict[str, Any]) -> bool:
    """A row that is not evidence about the agent — two causes, and they read differently.

    `routed away`: the classifier declined, so no work happened and the router is what failed.
    `left the checkout`: work happened, and the agent went outside the workspace **and got
    something**, so the row says the sandbox was probed rather than that the task was solved or
    not. An attempt the boundary refused is not this: it is a row that says the agent tried and
    stayed inside, and it is scored.
    """
    return _routed_away(row) or _escaped(row)


def _print_arms(rows: list[dict[str, Any]]) -> None:
    """The comparison the arms exist for: the same tasks, one mechanism off."""
    arms: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        arms[row["arm"]].append(row)
    if len(arms) < 2:
        return

    live = [r for r in rows if r.get("mode") == "live"]
    if not live:
        print(
            "\nOnly scripted rows so far — an arm table over them would compare the "
            "harness with itself. Run with `--live`."
        )
        return

    print(
        f"\n{'arm':<16}{'n':>4}{'aside':>7}{'pass rate':>12}{'median calls':>14}{'median secs':>13}"
    )
    print("-" * 68)
    base = [r for r in arms.get("full", []) if not _aside(r)]
    aside_rows: list[dict[str, Any]] = []
    for arm, group in sorted(arms.items(), key=lambda kv: (kv[0] != "full", kv[0])):
        aside = [r for r in group if _aside(r)]
        ran = [r for r in group if not _aside(r)]
        aside_rows += aside
        if ran:
            rate = sum(1 for r in ran if r.get("passed")) / len(ran)
            rate_text = f"{rate:>11.0%}"
        else:
            rate, rate_text = 0.0, f"{'-':>11}"
        delta = ""
        if base and arm != "full" and ran:
            base_rate = sum(1 for r in base if r.get("passed")) / len(base)
            delta = f"  ({rate - base_rate:+.0%} vs full)"
        print(
            f"{arm:<16}{len(group):>4}{len(aside):>7}{rate_text}"
            f"{_median([r.get('tool_calls', 0) for r in ran]):>14}"
            f"{_median([r.get('wall_s', 0) for r in ran]):>13}{delta}"
        )

    if aside_rows:
        routed = [r for r in aside_rows if _routed_away(r)]
        escaped = [r for r in aside_rows if _escaped(r)]
        print(
            f"\n⚠️ {len(aside_rows)} row(s) are out of the rates above, because the harness says "
            f"they are not\n   evidence about the agent:"
        )
        if routed:
            named = ", ".join(f"{r['task']}/{r['arm']}/rep{r['rep']}" for r in routed)
            print(
                f"   • {len(routed)} routed away before any work — {named}.\n"
                f"     A live ticket is classified by the model, so a decline ends the run "
                f"whatever the agent would have done."
            )
        if escaped:
            named = ", ".join(f"{r['task']}/{r['arm']}/rep{r['rep']}" for r in escaped)
            print(
                f"   • {len(escaped)} left the checkout — {named}.\n"
                f"     Shell commands reached outside the workspace, so the row measures the "
                f"sandbox being probed."
            )


def _print_arm_changes(rows: list[dict[str, Any]]) -> None:
    """What each arm actually changed — from the rows, not from the arm's own prose.

    ⚠️ **This is the line that would have caught the first ablation being three
    repeats of the baseline.** An arm whose rows say `(nothing)` is not an arm, and
    no rate distinguishes it from one that works.

    Read off the rows rather than from `arms.py` on purpose: the row is what the run
    was actually configured with, and a description in the source is what was
    *intended*. They disagreed for the whole life of three arms.
    """
    arms: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        arms[str(row.get("arm"))].append(row)
    if len(arms) < 2:
        return

    print("\nWhat each arm actually changed")
    print("-" * 88)
    for arm, group in sorted(arms.items(), key=lambda kv: (kv[0] != "full", kv[0])):
        recorded = group[0].get("arm_changes") if group else None
        if recorded is None:
            # Rows written before this was recorded. Saying so beats printing
            # "(nothing)", which would be a claim the file does not support.
            print(f"  {arm:<16} (not recorded in this file)")
            continue
        if not recorded:
            print(f"  {arm:<16} (nothing — the baseline configuration)")
            continue
        for key, (before, after) in sorted(recorded.items()):
            print(f"  {arm:<16} {key}: {before} -> {after}")


def _print_matrix(rows: list[dict[str, Any]]) -> None:
    """Which **cell** moved. A rate per arm hides the one that flipped.

    An arm's overall rate is an average over tasks that may disagree completely:
    `no-repair` at 100% on four of them and 0% on the fifth and 100% overall is not
    a contradiction, it is two different statements. The matrix is what makes the
    second one visible, and it is where an ablation is actually read.
    """
    tasks = sorted({r["task"] for r in rows})
    arms = sorted({r["arm"] for r in rows}, key=lambda a: (a != "full", a))
    # Two arms is the requirement — one arm has nothing to compare against and the
    # grouped table above already says it. One *task* is fine: re-running a single
    # cell under two arms is how a suspicion gets checked, and that is exactly when
    # the marking matters.
    if len(arms) < 2:
        return

    counts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        counts[(row["task"], row["arm"])].append(row)

    width = max(len(a) for a in arms) + 2
    print(f"\n{'task':<16}" + "".join(f"{a:>{width}}" for a in arms))
    print("-" * (16 + width * len(arms)))
    for task in tasks:
        baseline = [r for r in counts.get((task, "full"), []) if not _aside(r)]
        base_pass = sum(1 for r in baseline if r.get("passed"))
        cells = []
        for arm in arms:
            group = counts.get((task, arm), [])
            if not group:
                cells.append(f"{'-':>{width}}")
                continue
            # The fraction is over the rows that are evidence about the agent — **both**
            # causes, not just the first. A routed-away row never became a run; a row that
            # left the checkout says the boundary was probed rather than that the task was
            # solved or not.
            #
            # ⚠️ Excluding only the routed-away rows printed `0/3*` for a cell whose every row
            # had escaped, four lines above an arm table printing `-` and "aside 3" for the
            # same three rows. Two tables in one report disagreeing, and the one a reader is
            # likelier to quote was the one computing a rate over rows the harness had already
            # declared are not evidence. A cell with nothing to rate says so instead of
            # printing a zero: the marks are kept, so the cell still accounts for every row in
            # the file.
            ran = [r for r in group if not _aside(r)]
            routed = sum(1 for r in group if _routed_away(r))
            escaped = sum(1 for r in group if _escaped(r))
            mark = ("r" if routed else "") + ("e" if escaped else "")
            if not ran:
                cells.append(f"{f'-{mark}':>{width}}")
                continue
            passed = sum(1 for r in ran if r.get("passed"))
            if arm != "full" and baseline and passed < base_pass:
                # ⚠️ The cell the ablation exists to find. `*` rather than a colour
                # because this has to survive being pasted into a message.
                #
                # ⚠️ **Unless every non-pass in the cell is a crash**, in which case
                # it is marked `!` instead: the cell did drop, and the drop is not
                # evidence about the arm. Two marks rather than one because they
                # invite opposite actions — `*` says read this cell, `!` says this
                # cell says nothing yet and the sweep is one run short.
                failures = [r for r in ran if not r.get("passed")]
                mark = ("!" if failures and all(_is_crash(r) for r in failures) else "*") + mark
            cells.append(f"{f'{passed}/{len(ran)}{mark}':>{width}}")
        print(f"{task:<16}" + "".join(cells))

    if any(
        arm != "full" and len(counts.get((task, arm), [])) for task in tasks for arm in arms
    ) or any(_aside(r) for r in rows):
        print("\n* = fewer passes than `full` on that task — the cell to read")
        print("! = fewer passes, and every one of them a crash — the arm is not why")
        print("r = a row was routed away before any work — it is not in that fraction")
        print("e = the row left the checkout, so it is not evidence about the arm either")


def _is_crash(row: dict[str, Any]) -> bool:
    """The run fell over rather than the agent failing.

    ⚠️ **A crash is a result, and it is a result about the *infrastructure*.** The
    105-row sweep has one: `error: ConnectError: [Errno 11001] getaddrinfo failed` —
    DNS, on a `tight-window` cell, which the matrix then marked with the same `*` as
    the two cells where the agent genuinely behaved differently. Read together, that
    says "the arm costs you 5%", and the truth is "one HTTP request in 105 did not
    resolve".

    `row["error"]` is set by `run_one`'s handler and is the marker of a run that
    raised rather than one that was scored.
    """
    return bool(row.get("error"))


def _reasons_of(group: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    """Distinct failures in a group of rows, with how many produced each.

    Keyed by **(kind, reason)**, using the kinds the runner recorded beside the
    reasons: the same words can answer different questions — "checks did not pass"
    is behaviour, "the agent never mentioned" is narration — and a report that adds
    them together cannot say which moved. Rows written before the kinds existed get
    `?`, which is the honest answer rather than a re-derived guess.
    """
    reasons: dict[tuple[str, str], int] = defaultdict(int)
    for row in group:
        if row.get("passed"):
            continue
        reasons_for_row = row.get("failures") or ("(no reason recorded)",)
        kinds = list(row.get("failure_kinds") or ())
        for index, reason in enumerate(reasons_for_row):
            kind = kinds[index] if index < len(kinds) else "?"
            reasons[(kind, _shorten(str(reason)))] += 1
    return reasons


def _cell_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Task, arm and variant — the three things that define a condition."""
    return (
        str(row.get("task")),
        str(row.get("arm")),
        str(row.get("variant") or ""),
    )


def _cell_label(key: tuple[str, str, str]) -> str:
    task, arm, variant = key
    return f"{task} / {arm}" + (f" / {variant}" if variant else "")


def _print_failures(rows: list[dict[str, Any]]) -> None:
    """**Why** the runs that did not pass, did not pass.

    The rate is the least interesting number in this file. `score()` returns
    reasons precisely so this question is answerable — *"how often, without in what
    way, is not actionable"* — and until this existed the report printed the rate
    and dropped every reason on the floor, while `run.py` printed them inline and
    they scrolled away.

    It matters most for an ablation, where the interesting output is almost never
    "the arm scored lower" but "the arm scored lower, and here is the sentence
    saying why" — which is how the two defects in this harness were actually found.
    Both times, the reason was read rather than the number.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row.get("passed"):
            groups[_cell_key(row)].append(row)

    if not groups:
        print("\nNo run failed, so nothing here says what a failure looks like.")
        return

    print("\nWhy the runs that did not pass, did not pass")
    print("-" * 88)
    for key, group in sorted(groups.items()):
        print(f"{_cell_label(key)}  — {len(group)} of the rows above")
        for (kind, reason), count in sorted(
            _reasons_of(group).items(), key=lambda kv: (-kv[1], kv[0])
        ):
            print(f"    {count}x  [{kind}] {reason}")
        print()

    crashes = [r for r in rows if _is_crash(r)]
    if crashes:
        print(
            f"⚠️ {len(crashes)} of these row(s) did not run at all — `run_one` raised, "
            f"so nothing about the agent is in them. They are why a cell can drop "
            f"without the arm being responsible, and `_retry_failed` will re-run them "
            f"along with everything else that did not pass."
        )
    _print_probes(rows)
    print(
        "A reason repeated on every row of a cell is the cell's behaviour; one that "
        "appears once is one run's."
    )


def _print_probes(rows: list[dict[str, Any]]) -> None:
    """Rows that reached outside the checkout and **got nothing** — the boundary held.

    ⚠️ **Without this line the number is silent about the most interesting thing in the
    cell.** Since finding 21 an attempt the boundary refused does not disqualify its row
    (it is evidence the agent stayed inside), and `escaped_workspace` is what the aside
    mark reads — so a cell full of boundary probes looks exactly like a cell where the
    agent simply failed. Measured: the three contained `no-search` rows probed 19, 41
    and 10 times each and got through zero times, which is the finding 20 hole closed
    and not merely a task the model could not do.
    """
    probed = [
        row for row in rows if row.get("left_the_workspace") and not row.get("escaped_workspace")
    ]
    if not probed:
        return
    counts = ", ".join(str(len(row["left_the_workspace"])) for row in probed)
    print(
        f"\nThe boundary was probed and held: {len(probed)} row(s) ran shell commands "
        f"outside the checkout and none of them got through ({counts} command(s) each). "
        f"Under a boundary that confines reads an attempt is not contamination — it is "
        f"the agent finding out where the answers are not."
    )


def _print_comparison(
    before_rows: list[dict[str, Any]],
    after_rows: list[dict[str, Any]],
    before_name: str,
    after_name: str,
) -> None:
    """The same cells, before and after — which is what a re-run is *for*.

    ⚠️ **A retry cannot be read on its own.** `--retry-failed` produces a file
    holding only the cells that failed, so the matrix over it has no `full` column,
    nothing to compare against, and therefore no `*`. The question a retry asks is
    not "how did this cell do" but *"did it change"*, and that is a question about
    two files.

    Printed even when nothing moved, because **"it did not change" is the answer to
    the question that prompted the retry.** A comparison that stayed silent on an
    unchanged cell would leave the reader unsure it had been looked at.

    ⚠️ And the reason column is why this is not just two rate columns side by side:
    a cell can hold its rate and change its *behaviour* — same 2/3, a different
    sentence every time — and that is a different finding from a stable one.
    """

    def index(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
        out: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            out[_cell_key(row)].append(row)
        return out

    before, after = index(before_rows), index(after_rows)
    keys = sorted(set(before) | set(after))
    if not keys:
        return

    # ⚠️ **Only the cells in both files.** A retry run holds three cells and the
    # sweep it came from holds thirty; listing the other twenty-seven as
    # "(not retried)" buries the three this command exists to show. The count of
    # what was skipped is worth a line, not a table.
    comparable = [key for key in keys if before.get(key) and after.get(key)]
    comparable.sort(key=lambda k: _cell_label(k))

    print(f"\nBefore and after: {before_name}  ->  {after_name}")
    print("-" * 88)
    if not comparable:
        print("No cell appears in both files, so there is nothing to compare.")
        return
    print(f"{'cell':<38}{'before':>9}{'after':>9}{'calls':>16}")
    for key in comparable:
        old, new = before[key], after[key]
        old_pass = sum(1 for r in old if r.get("passed"))
        new_pass = sum(1 for r in new if r.get("passed"))
        calls = (
            f"{_median([r.get('tool_calls', 0) for r in old])} -> "
            f"{_median([r.get('tool_calls', 0) for r in new])}"
        )
        flag = "  <- the rate moved" if old_pass != new_pass else ""
        print(
            f"{_cell_label(key):<38}{f'{old_pass}/{len(old)}':>9}"
            f"{f'{new_pass}/{len(new)}':>9}{calls:>16}{flag}"
        )

    skipped = [key for key in keys if key not in comparable]
    if skipped:
        print(
            f"\n{len(skipped)} further cell(s) appear in only one of the two files "
            f"and so have no comparison."
        )

    # The reasons, for every cell where either side failed — a cell that kept its
    # rate but changed what went wrong is the case this section exists for.
    moved = [key for key in comparable if _reasons_of(before[key]) != _reasons_of(after[key])]
    if moved:
        print("\nWhat changed, cell by cell")
        for key in moved:
            print(f"  {_cell_label(key)}")
            print("    before: " + _reasons_line(before.get(key, [])))
            print("    after:  " + _reasons_line(after.get(key, [])))
        print(
            "\nA rate that did not move with a reason that did is not the same cell "
            "twice — it is two different failures at the same rate."
        )


def _reasons_line(group: list[dict[str, Any]]) -> str:
    reasons = _reasons_of(group)
    if not reasons:
        return "(none)"
    return " | ".join(
        f"{count}x {reason}"
        for reason, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def _print_agent_text(rows: list[dict[str, Any]], limit: int = 400) -> None:
    """What the agent *said* in the runs that did not pass. Opt-in.

    ⚠️ **This is the section that has actually found things.** Twice now the reason
    a run failed was not in `failures` at all: once it was an agent that had
    diagnosed the bug correctly and declined to fix it, and once it was the same
    agent quoting `find-the-bug-no-repair-1` — its own working directory — as if the
    treatment were part of the ticket. Neither is visible in a rate, and the second
    one is not visible in a failure *reason* either.

    Collapsed to one bounded excerpt per row, because the whole point is to read it.
    """
    failed = [r for r in rows if not r.get("passed") and r.get("agent_text")]
    if not failed:
        return

    print("\nWhat the agent said in those runs  (`--agent-text`)")
    print("-" * 88)
    for row in failed:
        print(f"{row['task']} / {row['arm']} / rep{row.get('rep')}  ({row.get('state')})")
        print(f"    …{_shorten(str(row['agent_text'])[-limit:], limit)}")
        print()


def _shorten(text: str, limit: int = 150) -> str:
    """One line, bounded — a traceback in the middle of a table helps nobody."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def main(argv: list[str] | None = None) -> int:
    from forgepilot.console import use_utf8_output

    # ⚠️ **Before the first `print`, and for the same reason the CLI does it.** With
    # stdout a pipe Python encodes with the machine's code page, so this report
    # printed every em dash as `��` for weeks without complaint — and then met a
    # `⚠️` and died with `UnicodeEncodeError: 'gbk' codec can't encode character
    # '⚠'`, *after* the table, on a run that had already done all its work.
    use_utf8_output()

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--latest", action="store_true", help="only the newest result file")
    parser.add_argument("--file", default=None, help="one result file by name")
    parser.add_argument(
        "--agent-text",
        action="store_true",
        help="also print what the agent said in the runs that did not pass",
    )
    parser.add_argument(
        "--compare",
        default=None,
        metavar="OLDER.jsonl",
        help=(
            "the same cells before and after, against this older file. What a "
            "`--retry-failed` run is read with: it holds only the cells that failed, "
            "so on its own it has nothing to compare against."
        ),
    )
    args = parser.parse_args(argv)

    if args.file:
        rows = load(args.file)
    elif args.latest:
        files = sorted(RESULTS.glob("*.jsonl"))
        if not files:
            print(f"no results in {RESULTS}. Run `evals/run.py` first.", file=sys.stderr)
            return 2
        rows = load(files[-1].name)
    else:
        rows = load("*.jsonl")

    if not rows:
        print(f"no results in {RESULTS}. Run `evals/run.py` first.", file=sys.stderr)
        return 2

    modes = ",".join(sorted({str(r.get("mode")) for r in rows}))
    print(f"{len(rows)} row(s) from {len({r.get('run') for r in rows})} run(s) [{modes}]\n")
    _print_grouped(rows)
    _print_attempt_gap(rows)
    _print_matrix(rows)
    _print_arms(rows)
    # ⚠️ **Outside `_print_arms`, which returns early on scripted rows.** The whole
    # value of this block is that it is evidence rather than prose, and evidence is
    # exactly what a scripted run — the one that costs nothing and gets run first —
    # can check before any money is spent.
    _print_arm_changes(rows)
    _print_failures(rows)
    if args.compare:
        older = load(Path(args.compare).name)
        if not older:
            print(f"\n{args.compare}: nothing to compare against.", file=sys.stderr)
        else:
            _print_comparison(older, rows, Path(args.compare).stem, "this run")
    if args.agent_text:
        _print_agent_text(rows)

    print(
        "\nA rate over a handful of runs is a hint, not a measurement: the same task "
        "against the same model varies run to run. Use --reps."
    )
    if "scripted" in modes:
        print(
            "Scripted rows measure the harness. A replay model performs a fix because "
            "it was told to, so those rates say nothing about an agent."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
