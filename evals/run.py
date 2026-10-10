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
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import arms as arm_module  # noqa: E402
import harness as H  # noqa: E402

RESULTS = H.HERE / "results"


async def run_one(task: H.EvalTask, arm: arm_module.Arm, rep: int, *, scripted: bool) -> dict:
    """Prepare, boot, plan, run, and reduce to a row. Never raises.

    A crashing run is a *result* — "the harness fell over on this task" is
    something the report must be able to show, and swallowing it into a missing
    row would make a broken arm look like a small sample.
    """
    from forgepilot.identity import ProjectPolicy, derive
    from forgepilot.kernel import dispose_fiber
    from forgepilot.orchestration import ClassifierGateway
    from forgepilot.orchestration.modes.task import plan_task, run_task
    from forgepilot.runtime.assembly import assemble_session, usage_tracker
    from forgepilot.seams.sandbox import SandboxEnforcement

    scratch = H.scratch_dir(task, arm.name, rep)
    root = None
    runtime = None
    started = time.monotonic()
    row: dict[str, Any] = {
        "task": task.name,
        "arm": arm.name,
        "rep": rep,
        "mode": "scripted" if scripted else "live",
        "started": datetime.now(timezone.utc).isoformat(),
    }

    try:
        repo = H.prepare_repo(task, scratch / "repo")
        shipped = H.base_paths(repo)
        H.export_environment(repo, scratch / "worktrees")
        root, _ = await H.boot(task, list(arm.patches), scripted=scripted)
        if scripted and task.script:
            root.llm.script(*H.script_turns(task))

        # ⚠️ **With the arm.** Without it, an arm that switches something off in the
        # *project* — which is three of the five — runs the baseline configuration,
        # and the comparison is the baseline against itself.
        project = H.write_project(task, scratch / "config", arm=arm)
        record = root.principal.resolve("demo-token")
        constraints = derive(record.principal, record.grant, project.to_policy(), key=None)

        runtime = await assemble_session(
            root,
            constraints,
            workspace_root=repo,
            principal=record.principal,
            minimum_enforcement_for_exec=SandboxEnforcement.NONE,
        )

        gateway = ClassifierGateway(root.classifier)
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
                changed=[], wall_s=round(time.monotonic() - started, 2), **{
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
        )

        final = outcome.checks[-1] if outcome.checks else None
        added = tuple(p for p in outcome.changed_paths if p not in shipped)
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
            _from_mapping(H.project_mapping(task), name=task.name).tests_in(outcome.changed_paths)
        )
        row.update(
            state=str(outcome.state),
            delivered=outcome.state.value in {"delivered", "ready"},
            checks_passed=bool(final and final.passed),
            checks_could_not_run=bool(final and final.could_not_run),
            pinned=list(outcome.pinned_tests),
            changed=list(outcome.changed_paths),
            diff_lines=len(outcome.diff.splitlines()),
            branch=outcome.branch,
            attempts=outcome.attempts,
            **H.summarise_session(runtime.session),
        )
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
                row["left_the_workspace"] = list(
                    H.tool_calls_that_left_the_workspace(
                        runtime.session.events,
                        # The scratch *root*, not this attempt's directory. Sibling
                        # attempts hold other tasks' fixtures and no answers, so
                        # allowing them costs nothing and removes a whole class of
                        # false positive — a command that navigates within the
                        # scratch area is not what this is looking for.
                        scratch=H.scratch_root(),
                        interpreter=sys.executable,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # Recorded as a finding, not swallowed. A detector that broke and
                # reported nothing would be indistinguishable from a clean run —
                # which is the failure this whole mechanism exists to stop.
                row["left_the_workspace"] = [f"detector failed: {type(exc).__name__}: {exc}"]
            try:
                await runtime.close()
            except Exception:  # noqa: BLE001
                pass

    row.setdefault("changed", [])
    row.setdefault("pinned", [])
    row.setdefault("left_the_workspace", [])
    for key in ("steps", "turns", "tool_calls", "compactions", "shadowed"):
        row.setdefault(key, 0)
    row["wall_s"] = round(time.monotonic() - started, 2)
    row["failures"] = H.score(row, task)
    row["passed"] = not row["failures"]
    return row


async def main_async(args: argparse.Namespace) -> int:
    tasks = [
        H.with_variant(H.load_task(n), args.variant)
        for n in (args.task or H.available_tasks())
    ]
    chosen = [arm_module.arm(n) for n in (args.arm or ["full"])]

    if not args.scripted and not args.live:
        print(
            "error: pick a mode. `--live` runs the shipped profile's real model "
            "(costs money, needs a credential); `--scripted` proves the harness "
            "works and measures nothing about the agent.",
            file=sys.stderr,
        )
        return 2

    RESULTS.mkdir(parents=True, exist_ok=True)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{'scripted' if args.scripted else 'live'}"
    out = RESULTS / f"{run_id}.jsonl"

    total = len(tasks) * len(chosen) * args.reps
    done = 0
    with out.open("w", encoding="utf-8") as handle:
        for task in tasks:
            for arm in chosen:
                for rep in range(1, args.reps + 1):
                    row = await run_one(task, arm, rep, scripted=args.scripted)
                    row["run"] = run_id
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", action="append", help="repeatable; default is every task")
    parser.add_argument("--arm", action="append", help="repeatable; default is `full`")
    parser.add_argument("--reps", type=int, default=1, help="runs per (task, arm)")
    parser.add_argument("--variant", default=None, help="a named override from the task spec")
    parser.add_argument("--live", action="store_true", help="the real model")
    parser.add_argument("--scripted", action="store_true", help="the replay model; a smoke test")
    args = parser.parse_args(argv)

    from forgepilot.env import load_local_env

    load_local_env(H.FORGE)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
