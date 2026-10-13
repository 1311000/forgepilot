"""Run eval tasks and append one JSONL row per attempt.

    # the smoke test: no network, no credential, proves the machine works
    python evals/run.py --task bug-fix --scripted --reps 1

    # the real thing: the shipped profile's own model, which is what a person runs
    python evals/run.py --live --reps 3

    # one mechanism at a time
    python evals/run.py --live --arm no-search --reps 3

Results land in `evals/results/<run-id>.jsonl` — one file per invocation, so two
runs can never be confused for one, and `report.py` reads them all.

**A row is not a verdict.** One run of one task says almost nothing on its own:
the same task against the same model varies. `--reps` exists because the variance
is the first thing worth knowing and the thing a single run hides.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import arms as arm_module  # noqa: E402
import harness as H  # noqa: E402

RESULTS = H.HERE / "results"

#: **Whether this run confines the model's commands.** Off by default, and that default is this
#: harness's own documented choice: an eval measures the *agent*, and a boundary that depends on the
#: host would make two machines' numbers incomparable. On is for the question the default cannot
#: answer — `no-search` only means "the answer is out of reach" if the shell cannot walk over to it,
#: and on a dev box whose sandbox reports only `partial` it can.
#:
#: `FORGEPILOT_EVAL_CONTAIN=1` is the whole switch. It swaps the sandbox row for the kernel one and
#: raises `minimum_enforcement_for_exec` to `FULL` — the same deliberate pair the CLI makes, except
#: that here the *run* is the thing being declared rather than the operator.
CONTAIN = os.environ.get("FORGEPILOT_EVAL_CONTAIN", "") not in ("", "0", "false", "False")


def _memory_count(root: Any) -> int:
    """How many memories the store holds right now — `0` when there is no store.

    Read through the service, and **not** through a counter this module keeps: the
    store is the thing that says what was learned, and a second count kept here is the
    second source of truth this project keeps finding defects in.
    """
    memory = getattr(root, "memory", None)
    if memory is None:
        return 0
    records = getattr(memory, "records", ())
    return len(records or ())


def _memory_for(root: Any, *, recall: bool) -> Any:
    """The store a turn may read, or `None` — which is a refusal, not a silence.

    `run_task(memory=None)` recalls nothing *and* says so: `Recaller` is not built, no
    `memory/recalled` event is written, and `full`/`no-memory` differ by exactly this
    argument. Passing the store and hoping for an empty result would make the arm a
    claim about the store's contents rather than about the mechanism.
    """
    return getattr(root, "memory", None) if recall else None


def _script_the_extraction(root: Any) -> None:
    """Answer the extraction prompt with the empty list, and nothing else.

    ⚠️ **Appending this to the agent's script does not work**, which is how it was
    written first: `H.script_turns(task)` scripts several repair attempts and a scripted
    run consumes only the first two or three, so the turns left over sit *in front of*
    the one meant for the distiller — the extraction prompt was answered with the
    agent's spare prose, and every scripted sweep reported `failed='schema'` for a
    reason that had nothing to do with distillation. The extraction is a separate
    prompt, so it gets a separate script: clear the queue and hand it exactly one
    answer. The honest answer a scripted model can give is "nothing", and scripting it
    means the step completes for real — the watermark advances and zero candidates is
    recorded as zero rather than as a transport failure. It is deliberately not a
    seeded memory: an eval that writes the memory it is about to measure has measured
    its own fixture.
    """
    root.llm.reset()
    root.llm.script(json.dumps([]))


async def _learn(
    root: Any,
    session: Any,
    project: Any,
    constraints: Any,
    record: Any,
) -> tuple[int | None, str]:
    """Distil the finished session into the arm's store. Returns `(written, failed)`.

    The scope and the tenant come from the *run* — the project's repository and the
    principal's tenant — because those are what make a memory attributable, and a
    distillation that guessed either would be writing a record no recall could scope.
    The budget is the run's own, so a maintenance pass cannot spend more than the task
    it is reading about.
    """
    from forgepilot.runtime.distillation import distill

    memory = getattr(root, "memory", None)
    watermarks = getattr(root, "distillationWatermarks", None)
    if memory is None or watermarks is None:
        return None, "no-memory-service"
    repo = project.repos[0] if project.repos else None
    if repo is None:
        return None, "no-scope"
    report = await distill(
        session,
        llm=root.llm,
        memory=memory,
        watermarks=watermarks,
        scope_ref=repo.slug,
        tenant_id=str(record.principal.tenant_id),
        repo=repo,
        model=getattr(root.llm, "model", "") or "",
        budget=constraints.budget,
    )
    return report.written, report.failed


async def run_one(
    task: H.EvalTask, arm: arm_module.Arm, rep: int, *, scripted: bool, contain: bool = False
) -> dict:
    """Prepare, boot, plan, run, and reduce to a row. Never raises.

    A crashing run is a *result* — "the harness fell over on this task" is
    something the report must be able to show, and swallowing it into a missing
    row would make a broken arm look like a small sample.
    """
    from forgepilot.identity import derive
    from forgepilot.orchestration import ClassifierGateway
    from forgepilot.orchestration.modes.task import plan_task, run_task
    from forgepilot.runtime.assembly import assemble_session, usage_tracker
    from forgepilot.seams.sandbox import SandboxEnforcement

    scratch = H.scratch_dir(task, arm.name, rep)
    # ⚠️ **The durable store is part of the harness, not of an arm.** Every arm gets the
    # same one (per task and arm), so `full` and `no-memory` differ in exactly one
    # thing: whether the run reads it. Giving only the memory arms a durable store would
    # make the pair compare a store against no store and call the difference "recall".
    memory_rows = H.memory_rows(task, arm.name)
    if rep == 1:
        H.reset_memory(task, arm.name)
    root = None
    runtime = None
    started = time.monotonic()
    row: dict[str, Any] = {
        "task": task.name,
        "arm": arm.name,
        "rep": rep,
        "mode": "scripted" if scripted else "live",
        # A row that does not say whether a boundary was in force cannot be read later: the whole
        # difference between "the agent could not find it" and "the agent was not allowed to go and
        # look" is this boolean.
        "contained": contain,
        # What the two memory switches were, on the row as well as in `arm_changes`:
        # the diff is the evidence, and this is the thing a reader filters on.
        "memory_on": bool(arm.memory),
        "distill_on": bool(arm.distill),
        "started": datetime.now(UTC).isoformat(),
        # The mapping from an opaque directory back to what it was. It lives here
        # rather than in the path because the path is inside the agent's own
        # context, and a directory called `...-no-repair-1` is a hint the model can
        # read — and did.
        "scratch": str(scratch),
    }

    try:
        # **Every repository the task needs.** `prepare_repos` returns the primary
        # checkout and one more per `extra_repos`, and the export below names each
        # one under the variable the task's own patch rows expand — a task that
        # measures a plural ticket (`two-repos`) is the only one with extras.
        repo, extra_repos = H.prepare_repos(task, scratch / "repo")
        shipped = H.base_paths(repo)
        H.export_environment(repo, scratch / "worktrees", extras=extra_repos)
        root, _ = await H.boot(
            task, [*arm.patches, *memory_rows], scripted=scripted, contain=contain
        )
        if scripted and task.script:
            turns = list(H.script_turns(task))
            root.llm.script(*turns)

        # ⚠️ **With the arm.** Without it, an arm that switches something off in the
        # *project* — which is three of the five — runs the baseline configuration,
        # and the comparison is the baseline against itself.
        # **What the store already held when this attempt started.** Read after boot and
        # before the first turn, because that is the only moment the number means "what
        # this run could have recalled" — after the run it also counts what the run
        # itself wrote, and the two are the difference between a warm store and a busy
        # one. Reps of one arm share a store, so rep 1 is 0 by construction and a nonzero
        # number here is the learning the earlier reps left behind.
        row["memories_before"] = _memory_count(root)
        project = H.write_project(task, scratch / "config", arm=arm)
        record = root.principal.resolve("demo-token")
        constraints = derive(record.principal, record.grant, project.to_policy(), key=None)

        runtime = await assemble_session(
            root,
            constraints,
            workspace_root=repo,
            principal=record.principal,
            minimum_enforcement_for_exec=(
                SandboxEnforcement.FULL if contain else SandboxEnforcement.NONE
            ),
        )

        gateway = ClassifierGateway(root.classifier, session=runtime.session)
        plan = await plan_task(
            ticket=H.ticket_for(task),
            project=project,
            constraints=constraints,
            registry=root.tools,
            classifier=gateway,
            workspace_root=repo,
        )
        row["plan_state"] = str(plan.state)
        row["classifier_calls"] = gateway.calls
        if not plan.ready:
            row.update(
                state=str(plan.state), delivered=False, checks_passed=False,
                changed=[], wall_s=round(time.monotonic() - started, 2),
                # Empty rather than absent: a plan that never became ready made no
                # attempt at the check, and `any([])` is False — the honest reading.
                # A *missing* key means an old results file, and the report says so.
                check_verdicts=[],
                # No tree was worked on, so no held-out input can say anything.
                # `score` only looks at probes when the checks passed, so this is
                # never the reason a refused plan failed — its state is.
                probes=[], **{
                    k: 0 for k in ("steps", "turns", "tool_calls", "compactions", "shadowed")
                },
            )
            row["failures"] = H.score(row, task)
            # Set here as well as on the full path. It was not, and a *correct*
            # `awaiting_info` — the outcome this whole task exists to measure —
            # printed as FAIL with no reasons, because `passed` was simply absent
            # and the printer read a missing key as a failure.
            row["passed"] = not row["failures"]
            return row

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
            policy=root.policy,
            approval=root.approval,
            principal=record.principal,
            compactor=getattr(root, "compaction", None),
            # On by default here, because an eval that runs without the mechanism is
            # measuring a system nobody deploys. The `no-memory` arm is the *deliberate*
            # version of that: it passes `None`, so the difference between the two arms
            # is one argument and not a configuration that nobody reads.
            memory=_memory_for(root, recall=arm.memory),
        )

        # **M13: one task, several deliveries.** The flat keys below stay what they
        # were for a single-repository task — which is every task written before
        # this, and every row recorded by them — because they are then that
        # delivery's own values. For a plural ticket they are the task's aggregate,
        # and the per-repository facts live in `deliveries`, where an expectation
        # about a plural ticket looks. Keeping the flat keys is not nostalgia: the
        # report groups and compares on them, and a schema that changes shape when a
        # task happens to span two repositories would make two eras uncomparable.
        deliveries = tuple(outcome.deliveries)
        single = deliveries[0] if len(deliveries) == 1 else None
        checks = (
            tuple(single.checks)
            if single is not None
            else tuple(check for d in deliveries for check in d.checks)
        )
        changed = (
            tuple(single.changed_paths)
            if single is not None
            else tuple(dict.fromkeys(path for d in deliveries for path in d.changed_paths))
        )
        diff = (
            single.diff
            if single is not None
            else "\n".join(f"# {d.repo.slug}\n{d.diff}" for d in deliveries if d.diff)
        )
        pinned_tests = (
            tuple(single.pinned_tests)
            if single is not None
            else tuple(dict.fromkeys(path for d in deliveries for path in d.pinned_tests))
        )
        final = checks[-1] if checks else None
        # Recorded per repository as well as in aggregate: the question a plural
        # ticket asks is not "did the task pass" but "did **each** repository's
        # delivery pass, and which ones were never attempted".
        row["deliveries"] = [
            {
                "repo": d.repo.slug,
                "state": d.state.value,
                "attempted": d.attempted,
                "branch": d.branch,
                "changed": list(d.changed_paths),
                "checks_passed": bool(d.checks[-1].passed) if d.checks else False,
                "pinned": list(d.pinned_tests),
                "pull_request": d.pull_request.url if d.pull_request is not None else "",
                "reason": d.reason,
            }
            for d in deliveries
        ]
        # ⚠️ **Per attempt, because one number hides the mechanism.** A row carrying
        # only the *final* verdict cannot tell a fix that worked first time from one
        # the repair loop rescued — and "the second round is what turns one into the
        # other" is the entire claim the loop rests on. `task.py` appends exactly one
        # check per attempt, in order, so index 0 is the first attempt's verdict:
        # that is pass@1, and the last is what the run delivered. For a plural ticket
        # this is every delivery's attempts end to end, and `deliveries` carries each
        # one's own verdicts.
        #
        # Absent from rows recorded before this existed. The report prints `-` for
        # those rather than a rate of zero, because "no attempt passed" and "nobody
        # wrote it down" are different facts.
        row["check_verdicts"] = [bool(c.passed) for c in checks]
        added = tuple(p for p in changed if p not in shipped)
        row["added_paths"] = list(added)
        row["added_tests"] = list(project.tests_in(added))
        # What counts as a test comes from the **task's** project, not the arm's:
        # the `no-pinning` arm empties `test_paths` on purpose, and letting that
        # redefine the expectation would make the task fail for a reason that has
        # nothing to do with what it measures.
        from forgepilot.config.project import project_from_mapping as _from_mapping

        # Everything the agent *said*, for the expectation that is about whether
        # it told you something. Over the whole conversation, not the last
        # message: the last turn of a run is often a tool call with no prose at
        # all, and reading only that reports "it never said" about a run that said
        # so twice.
        row["agent_text"] = "\n".join(
            str(e.data.get("text", ""))
            for e in runtime.session.committed_events
            if e.kind == "assistant/message" and e.data.get("text")
        )
        row["changed_tests"] = list(
            _from_mapping(H.project_mapping(task), name=task.name).tests_in(changed)
        )
        # ⚠️ **The half this eval never had.** `memory=` wired *recall*, and nothing ever
        # wrote into the store — so every memory arm began empty and the comparison was
        # between two runs that had both learned nothing, which looks exactly like a
        # mechanism that makes no difference. Distillation runs here, after the checks,
        # because it is a maintenance pass over a finished session and not a turn: it
        # must change what the *next* attempt starts from, never what this one did.
        row["distilled"] = None
        row["distill_failed"] = ""
        if arm.distill:
            if scripted:
                _script_the_extraction(root)
            written, failed = await _learn(
                root, runtime.session, project, constraints, record
            )
            row["distilled"] = written
            row["distill_failed"] = failed
        row.update(
            state=str(outcome.state),
            delivered=outcome.state.value in {"delivered", "ready"},
            checks_passed=bool(final and final.passed),
            checks_could_not_run=bool(final and final.could_not_run),
            pinned=list(pinned_tests),
            changed=list(changed),
            diff_lines=len(diff.splitlines()),
            # Empty for a plural ticket, deliberately: no single branch names where
            # two repositories' changes went, and picking one would be the same
            # lie the deleted `TaskOutcome.branch` field would have told.
            branch=single.branch if single is not None else "",
            attempts=outcome.attempts,
            **H.summarise_session(runtime.session),
        )
        # **After the run, against what it left behind.** The held-out inputs only
        # mean something once the tree is final, and they are the only instrument
        # here that can see a cheat which never touched a test — see
        # `harness.run_probes`. ⚠️ And "what it left behind" is the session's
        # *worktree*, not the origin checkout the fixture was copied to: probing the
        # origin reports a correct fix as a cheat. See `harness.working_tree`.
        row["probes"] = H.run_probes(H.working_tree(repo), task)
    except Exception as exc:  # noqa: BLE001 - a crash is a result, not a missing row
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["traceback"] = traceback.format_exc()[-1500:]
    finally:
        if runtime is not None:
            # **Here rather than on the success path.** A run that died mid-turn is
            # the one most likely to have wandered, and reading `committed_events`
            # would miss exactly the buffer a crash leaves behind. `session.events`
            # is the whole in-memory log.
            try:
                calls = H.calls_outside_the_workspace(
                    runtime.session.events,
                    # The scratch *root*, not this attempt's directory. Sibling
                    # attempts hold other tasks' fixtures and no answers, so
                    # allowing them costs nothing and removes a whole class of
                    # false positive — a command that navigates within the
                    # scratch area is not what this is looking for.
                    scratch=H.scratch_root(),
                    interpreter=sys.executable,
                    # ⚠️ `contain` is a claim about the **boundary**, and it decides whether
                    # any of these can be an escape. The containment layer pins the
                    # namespace sandbox with `read_scope: workspace`, and that provider
                    # refuses to execute unless its own probe verifies the claim — so a
                    # command that ran under it could not have read the eval's material:
                    # the material is **absent**, not read-only. Measured, because exit
                    # codes cannot see the difference: `ls -la /eval/` exits 0 and lists an
                    # empty mask. Uncontained rows keep the effect-aware verdict.
                    reads_confined=contain,
                )
                # Both keys, on purpose. `left_the_workspace` is every **attempt** — it
                # is what this detector has always recorded, and the attempt is worth
                # reading on its own (under a boundary that confines reads, an attempt
                # that was refused is evidence the agent *stayed* inside). Only
                # `escaped_workspace`, the attempts that got something, may take the
                # row out of the report: a command that printed nothing is not a reason
                # to discard the row. See finding 21.
                row["left_the_workspace"] = [call.describe() for call in calls]
                row["escaped_workspace"] = [call.describe() for call in calls if call.escaped]
            except Exception as exc:  # noqa: BLE001
                # Recorded as a finding, not swallowed. A detector that broke and
                # reported nothing would be indistinguishable from a clean run —
                # which is the failure this whole mechanism exists to stop.
                row["left_the_workspace"] = [f"detector failed: {type(exc).__name__}: {exc}"]
                # The *effect-aware* key too: a broken detector must leave the row
                # aside rather than scorable, or the defect would quietly turn every
                # contaminated row into a pass rate.
                row["escaped_workspace"] = list(row["left_the_workspace"])
            try:
                await runtime.close()
            except Exception:  # noqa: BLE001
                pass

    row.setdefault("changed", [])
    row.setdefault("pinned", [])
    row.setdefault("left_the_workspace", [])
    row.setdefault("escaped_workspace", [])
    row.setdefault("check_verdicts", [])
    # The held-out probes, run against the tree the agent left behind. Defaulted
    # rather than left absent so `score` can tell "this task holds nothing out"
    # from "the run forgot to say" — the second is a defect in this harness.
    row.setdefault("probes", [])
    for key in ("steps", "turns", "tool_calls", "compactions", "shadowed"):
        row.setdefault(key, 0)
    row["wall_s"] = round(time.monotonic() - started, 2)
    row["failures"] = H.score(row, task)
    row["passed"] = not row["failures"]
    return row


def failed_cells(path: Path) -> list[tuple[str, str, str | None]]:
    """The (task, arm, variant) cells in ``path`` that produced a row which did not pass.

    **A cell, not a row.** Three runs of one arm on one task are three samples of
    one condition; re-running two of them because those two failed would answer a
    different question from the one being asked. The question after a sweep is
    *"was that cell real?"*, and that is a question about the cell.

    Order is preserved and de-duplicated, so the retry runs in the order the
    failures were found.
    """
    import json as _json

    seen: list[tuple[str, str, str | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = _json.loads(line)
        if row.get("passed"):
            continue
        key = (str(row.get("task")), str(row.get("arm")), row.get("variant"))
        if key not in seen:
            seen.append(key)
    return seen


def latest_results() -> Path:
    files = sorted(RESULTS.glob("*.jsonl"))
    if not files:
        raise FileNotFoundError(f"no results in {RESULTS}. Nothing to retry.")
    return files[-1]


async def main_async(args: argparse.Namespace) -> int:
    if not args.scripted and not args.live:
        print(
            "error: pick a mode. `--live` runs the shipped profile's real model "
            "(costs money, needs a credential); `--scripted` proves the harness "
            "works and measures nothing about the agent.",
            file=sys.stderr,
        )
        return 2

    if args.retry_failed:
        source = Path(args.retry_failed) if args.retry_failed != "-" else latest_results()
        if not source.is_file():
            source = RESULTS / source.name
        cells = failed_cells(source)
        if not cells:
            print(
                f"{source.name}: every row passed, so there is nothing to retry. "
                f"Run a sweep with `--arm` to produce some failures, or drop "
                f"`--retry-failed`.",
                file=sys.stderr,
            )
            return 2
        print(f"{source.name}: retrying {len(cells)} cell(s) that did not pass")
    else:
        cells = [
            (name, arm, args.variant)
            for name in (args.task or H.available_tasks())
            for arm in (args.arm or ["full"])
        ]

    # Loaded once per distinct name rather than once per cell: two arms over the
    # same task are the same task.
    tasks_by_name = {name: H.load_task(name) for name, _, _ in cells}

    RESULTS.mkdir(parents=True, exist_ok=True)
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{'scripted' if args.scripted else 'live'}"
    out = RESULTS / f"{run_id}.jsonl"

    total = len(cells) * args.reps
    print(
        f"{run_id}: {len(cells)} cell(s) x {args.reps} rep(s) = {total} row(s) -> {out.name}"
        + (
            "  ** contained: the kernel sandbox, and `exec` is refused without it **"
            if CONTAIN
            else ""
        )
    )
    done = 0
    with out.open("w", encoding="utf-8") as handle:
        for name, arm_name, variant in cells:
            task = H.with_variant(tasks_by_name[name], variant)
            arm = arm_module.arm(arm_name)
            for rep in range(1, args.reps + 1):
                row = await run_one(task, arm, rep, scripted=args.scripted, contain=CONTAIN)
                row["run"] = run_id
                # ⚠️ **What this row's arm actually changed**, recorded per row so a
                # results file carries its own evidence. An arm that switched nothing
                # off produces a row saying `[]`, which is distinguishable from an arm
                # that switched something off and made no difference — and for most of
                # this harness's life those two were the same table.
                row["arm_changes"] = {
                    key: [str(before), str(after)]
                    for key, (before, after) in H.arm_changes(task, arm).items()
                }
                # ⚠️ **Recorded, or a variant failing is unrepeatable.**
                # `--retry-failed` reads this back to know which *cell* to re-run,
                # and without it a failing `bug-fix/weakens-the-test` would come
                # back as plain `bug-fix` — a different question, silently.
                row["variant"] = variant
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                done += 1
                verdict = "pass" if row.get("passed") else "FAIL"
                print(
                    f"[{done}/{total}] {task.name:<12} {arm.name:<12} rep {rep}  "
                    f"{verdict:<4} {row.get('state', '-'):<10} "
                    f"{row.get('tool_calls', 0):>3} calls  {row.get('wall_s', 0):>6.1f}s"
                    + (f"  {row['failures']}" if row.get("failures") else "")
                )

    print(f"\nwrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from forgepilot.console import use_utf8_output

    # The run prints a task's own ticket text in its failure list, which is whatever
    # the ticket author typed — so this needs the same treatment as the report, and
    # for the same reason.
    use_utf8_output()

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", action="append", help="repeatable; default is every task")
    parser.add_argument("--arm", action="append", help="repeatable; default is `full`")
    parser.add_argument("--reps", type=int, default=1, help="runs per (task, arm)")
    parser.add_argument("--variant", default=None, help="a named override from the task spec")
    parser.add_argument("--live", action="store_true", help="the real model")
    parser.add_argument("--scripted", action="store_true", help="the replay model; a smoke test")
    parser.add_argument(
        "--retry-failed",
        nargs="?",
        const="-",
        default=None,
        metavar="RESULTS.jsonl",
        help=(
            "re-run only the (task, arm, variant) cells that produced a row which "
            "did not pass. Defaults to the newest results file. Overrides --task, "
            "--arm and --variant, which exist to describe a sweep and not a retry."
        ),
    )
    args = parser.parse_args(argv)

    from forgepilot.env import load_local_env

    load_local_env(H.FORGE)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
