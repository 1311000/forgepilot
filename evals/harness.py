"""Run one eval task under one arm, and record what happened.

**What this is for.** The design doc claims an end-to-end delivery improvement
over a generic coding-agent workflow. The doc itself concedes that the eval
platform, the success criteria, the sample size and the comparison conditions are
all unspecified — and `CLAUDE.md` says not to repeat the number until it is
something we measured. This is the thing that measures.

**What it is not.** It cannot produce a number *against Claude Code*: there is no
comparison arm available and inventing one would be the same unfounded claim in a
new place. What it produces is our own delivery rate, and — this is the part that
actually answers the design questions — **how much each mechanism contributes**,
by running the same tasks with one mechanism switched off at a time.

**Live by default, scripted for the smoke test.** The interesting numbers need a
real model; a scripted one performs a fix because it was told to, so it measures
the harness and not the agent. Scripted mode exists to prove the machine works
before spending anything, exactly as `tests/providers/test_github_vcs_live.py`
gates its network call.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
FORGE = HERE.parent
TASKS = HERE / "tasks"

#: The repository slug every eval task uses. One slug means one `project.yaml`
#: and one ticket convention; a task that needs different permissions overrides
#: the project rather than inventing a second world.
SLUG = "git.local/demo/stats"


@dataclass(frozen=True, slots=True)
class EvalTask:
    name: str
    directory: Path
    fixture: Path
    ticket: dict[str, Any]
    expect: dict[str, Any]
    project: dict[str, Any] = field(default_factory=dict)
    #: The offline half: what a fixed classifier should say, and the turns a
    #: scripted model should take. Absent means the task has no offline path.
    understanding: dict[str, Any] = field(default_factory=dict)
    script: tuple[dict[str, Any], ...] = ()
    #: Named overrides of `script` and `expect`. A variant exists to pin one
    #: *harness* behaviour — `feature`'s `cheating` variant is the case that
    #: decides whether the oracle pinning is looking at the feature at all.
    variants: dict[str, Any] = field(default_factory=dict)


def load_task(name: str) -> EvalTask:
    """Read ``tasks/<name>/task.yaml``. A missing or malformed spec raises.

    Loudly, because the alternative is an eval that silently runs zero tasks and
    reports a delivery rate over nothing.
    """
    directory = TASKS / name
    spec_path = directory / "task.yaml"
    if not spec_path.is_file():
        known = sorted(p.parent.name for p in TASKS.glob("*/task.yaml"))
        raise FileNotFoundError(f"no eval task {name!r}. Known: {known}")

    raw = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    fixture = Path(str(raw.get("fixture", "fixture")))
    if not fixture.is_absolute():
        fixture = (directory / fixture).resolve()
    if not fixture.is_dir():
        raise FileNotFoundError(f"{spec_path}: fixture {fixture} is not a directory")

    return EvalTask(
        name=str(raw.get("name", name)),
        directory=directory,
        fixture=fixture,
        ticket=dict(raw.get("ticket") or {}),
        expect=dict(raw.get("expect") or {}),
        project=dict(raw.get("project") or {}),
        understanding=dict(raw.get("understanding") or {}),
        script=tuple(raw.get("script") or ()),
        variants=dict(raw.get("variants") or {}),
    )


def with_variant(task: EvalTask, name: str | None) -> EvalTask:
    """The task with a named variant's overrides applied, or the task itself.

    A missing variant raises: a typo in `--variant` would otherwise run the base
    task and score it against the base expectations, which is a green result for
    a question that was never asked.
    """
    import dataclasses

    if not name:
        return task
    if name not in task.variants:
        raise KeyError(f"{task.name}: no variant {name!r}. Known: {sorted(task.variants)}")
    overrides = dict(task.variants[name])
    changes: dict[str, Any] = {}
    if "script" in overrides:
        changes["script"] = tuple(overrides["script"] or ())
    if "expect" in overrides:
        changes["expect"] = {**task.expect, **dict(overrides["expect"] or {})}
    return dataclasses.replace(task, **changes)


def available_tasks() -> tuple[str, ...]:
    return tuple(sorted(p.parent.name for p in TASKS.glob("*/task.yaml")))


def _git(*args: str, cwd: Path) -> None:
    completed = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}:\n{completed.stderr}")


def prepare_repo(task: EvalTask, into: Path) -> Path:
    """A fresh git repository from the task's fixture, ready to be a worktree
    source. Recreated every run: an eval that reuses a mutated checkout measures
    the previous run."""
    from forgepilot.fs import remove_tree

    if into.exists():
        remove_tree(into)
    into.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        task.fixture, into, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".git")
    )
    _git("init", "-b", "main", cwd=into)
    _git("add", "-A", cwd=into)
    _git(
        "-c", "user.name=ForgePilot Eval", "-c", "user.email=eval@localhost",
        "commit", "-m", f"eval fixture: {task.name}", cwd=into,
    )
    return into


def export_environment(repo: Path, worktrees: Path) -> dict[str, str]:
    """The variables `examples/stat-fix/patch.yaml` expands.

    Set rather than replaced, so the eval runs **the same patch file the README
    tells a person to run**. An eval that assembled its own plugin tree would be
    measuring a program nobody has.
    """
    values = {
        "FORGEPILOT_EXAMPLE_REPO": str(repo),
        "FORGEPILOT_EXAMPLE_WORKTREES": str(worktrees),
        "FORGEPILOT_PYTHON": sys.executable,
    }
    os.environ.update(values)
    return values


def project_mapping(task: EvalTask) -> dict[str, Any]:
    """The example's project with the task's own overrides — **and no arm**.

    Kept separate from `write_project` because the runner needs it that way: what
    counts as a test path is the task's declaration, and the `no-pinning` arm
    empties `test_paths` on purpose. Letting an arm redefine the expectation would
    make a task fail for a reason it does not measure.
    """
    base = yaml.safe_load(
        (FORGE / "examples" / "stat-fix" / "project.yaml").read_text(encoding="utf-8")
    )
    return {**base, **task.project}


def write_project(task: EvalTask, into: Path):
    """The example's project plus the arm's override, written out and loaded.

    ⚠️ **Through `load_project`, not `project_from_mapping`.** They differ in
    exactly one way and it bit immediately: only the loader expands `${VAR}`, and
    the example's `test:` command is `${FORGEPILOT_PYTHON} -m pytest -q`. Going
    straight to the mapping constructor left that literal, so the check ran
    `${FORGEPILOT_PYTHON}` as an executable, the sandbox reported "command not
    found", and every task came back `could_not_run` — the harness reporting a
    failure of the *project's* configuration when it was the harness that skipped
    a step.

    It also keeps the eval running the same program `--task` runs: `project.yaml`
    honours `${VAR}` because a check command is a *machine's* business, and skipping
    that step would quietly evaluate a configuration nobody uses.
    """
    from forgepilot.config.project import load_project

    into.mkdir(parents=True, exist_ok=True)
    path = into / "project.yaml"
    path.write_text(
        yaml.safe_dump(project_mapping(task), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return load_project(path)


async def boot(task: EvalTask, arm_patches: list[dict[str, Any]], *, scripted: bool):
    """Resolve the profile plus the example patch plus the arm, and load it.

    Three layers, in this order, and each one is a different kind of statement:

    1. the profile as shipped — bundles, then the user layer;
    2. `examples/stat-fix/patch.yaml`, by **path**, so the eval runs the same file
       the README tells a person to run;
    3. the arm, inline, because an arm is a thing this harness invents for
       comparison and does not deserve a committed file that looks like
       deployment config.
    """
    from forgepilot.config import apply_layers, compose_layers, load_profile
    from forgepilot.config.patch import patch_layer_from_mapping
    from forgepilot.kernel import create_root

    profile = load_profile(FORGE / "configs", "default")
    layers = list(
        compose_layers(profile, [FORGE / "examples" / "stat-fix" / "patch.yaml"])
    )

    if arm_patches:
        layers.append(patch_layer_from_mapping(f"--arm:{task.name}", list(arm_patches)))

    if scripted:
        # Two rows, and both are needed. `user.patch.yaml` swaps in the real
        # model, so the replay row puts the scripted one back; and the classifier
        # *also* runs on the model, so without the second row it declines every
        # ticket and every offline run stops at `awaiting_info` before it starts.
        rows: list[dict[str, Any]] = [
            {
                "id": "llm",
                "plugin": "forgepilot.providers.llm.replay:ReplayLLMPlugin",
                "config": {"model": "replay-1"},
                "tier": "trusted",
            }
        ]
        if task.understanding:
            rows.append(
                {
                    "id": "classifier",
                    "plugin": "fixed_classifier:FixedClassifierPlugin",
                    "config": dict(task.understanding),
                    "tier": "trusted",
                }
            )
        layers.append(patch_layer_from_mapping("--scripted", rows))

    resolved = apply_layers(tuple(layers), profile=profile.name)
    root = create_root()
    await resolved.materialize_async(root)
    return root, resolved


def script_turns(task: EvalTask) -> list[Any]:
    """The scripted model's turns, from the task's own solution.

    ``write`` reads a file out of the task directory and issues the ``write_file``
    call that produces it, so the solution lives as a readable file rather than as
    a YAML block scalar. ``say`` is a text-only turn.

    Padded with ``say`` turns: the repair loop asks the model again whenever a
    check fails, and a script that runs out raises rather than inventing a reply —
    which is right, but it would turn "this task needs two attempts" into "the
    harness crashed".
    """
    import json as _json

    from forgepilot.providers.llm.replay import text_turn, tool_call_turn

    turns: list[Any] = []
    for index, step in enumerate(task.script, start=1):
        if "write" in step:
            spec = step["write"]
            content = (task.directory / str(spec["from"])).read_text(encoding="utf-8")
            turns.append(
                tool_call_turn(
                    "write_file",
                    _json.dumps({"path": str(spec["path"]), "content": content}),
                    call_id=f"c{index}",
                )
            )
        elif "say" in step:
            turns.append(text_turn(str(step["say"])))
        else:
            raise ValueError(f"{task.name}: unrecognised script step {step!r}")
    turns.extend(text_turn("nothing further") for _ in range(4))
    return turns


def ticket_for(task: EvalTask):
    from forgepilot.seams.refs import TicketId
    from forgepilot.seams.tickets import Ticket, TicketPriority

    raw = task.ticket
    return Ticket(
        id=TicketId(str(raw.get("id", task.name))),
        title=str(raw.get("title", task.name)),
        description_md=str(raw.get("description", "")),
        priority=TicketPriority(str(raw.get("priority", "high"))),
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score(row: dict[str, Any], task: EvalTask) -> list[str]:
    """Why this row is not a pass. Empty list means it is one.

    Returns *reasons* rather than a boolean so a report can say what went wrong
    rather than only how often.
    """
    expect = task.expect
    failures: list[str] = []

    if row.get("error"):
        failures.append(f"error: {row['error']}")
        return failures

    states = expect.get("states")
    if states and row["state"] not in states:
        failures.append(f"state {row['state']!r} not in {states}")

    wanted = expect.get("checks")
    if wanted == "pass" and not row["checks_passed"]:
        failures.append("checks did not pass")
    if wanted == "fail" and row["checks_passed"]:
        failures.append("checks passed when they should not have")
    if row.get("checks_could_not_run"):
        failures.append("checks could not run")

    limit = expect.get("max_changed_paths")
    if limit is not None and len(row["changed"]) > limit:
        failures.append(f"{len(row['changed'])} paths changed, limit {limit}")
    limit = expect.get("max_tool_calls")
    if limit is not None and row["tool_calls"] > limit:
        failures.append(f"{row['tool_calls']} tool calls, limit {limit}")
    limit = expect.get("max_turns")
    if limit is not None and row["turns"] > limit:
        failures.append(f"{row['turns']} turns, limit {limit}")

    for path in expect.get("changed_paths_include") or ():
        if path not in row["changed"]:
            failures.append(f"{path!r} was not changed")

    # The one that matters for `out-of-scope`: a run may do anything except
    # touch a path it is not permitted to touch, and "it delivered nothing" is
    # not a pass if what it delivered was the forbidden change.
    for path in expect.get("changed_paths_exclude") or ():
        if path in row["changed"]:
            failures.append(f"{path!r} was changed and must not be")

    # A feature task's specification is a test, and **which** test is the agent's
    # business. The first live run wrote `tests/test_median.py` — a better name
    # than the one this expectation used to demand — and was marked a failure
    # three times out of three for it. Naming the file measures conformance to a
    # guess about the agent's vocabulary, not the task.
    #
    # "Changed" rather than "added", because extending an existing test file is
    # as valid as creating one and the `feature` fixture has a test file already.
    if expect.get("changes_tests") and not row.get("changed_tests"):
        failures.append(
            "no test changed, so nothing in the checks describes the feature"
        )

    # A boundary task that never met the boundary produced no evidence. That is a
    # failure of the *task*, and saying so is the only honest reading.
    floor = expect.get("min_guard_refusals")
    if floor is not None and row.get("guard_refusals", 0) < floor:
        failures.append(
            f"the guard was never exercised ({row.get('guard_refusals', 0)} refusal(s), "
            f"wanted {floor}) — this run says nothing about the boundary"
        )

    limit = expect.get("max_classifier_calls")
    if limit is not None and row.get("classifier_calls", 0) > limit:
        failures.append(f"{row['classifier_calls']} classifier calls, limit {limit}")

    return failures


def base_paths(repo: Path) -> frozenset[str]:
    """The files the fixture shipped with, so "added" can be told from "changed".

    A feature task's specification is a test that *did not exist*; a boundary
    task's evidence is a refusal. Neither can be read off the final file list
    alone.
    """
    completed = subprocess.run(
        ["git", "ls-files"], cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    return frozenset(line for line in (completed.stdout or "").splitlines() if line)


def summarise_session(session: Any) -> dict[str, Any]:
    """Counters read from the **log**, not from the caller's bookkeeping.

    The loop's own counters would be a second source of truth, and the whole
    reason this project keeps finding defects is that a second source of truth
    drifts.
    """
    from forgepilot.seams.session import EventKind

    events = session.committed_events
    return {
        "steps": sum(1 for e in events if e.kind == EventKind.STEP_START),
        "turns": sum(1 for e in events if e.kind == EventKind.TURN_START),
        "tool_calls": sum(1 for e in events if e.kind == EventKind.TOOL_CALL),
        "compactions": sum(1 for e in events if e.kind == EventKind.COMPACTION_END),
        # Evidence that the *permission boundary* was actually met. Without it a
        # boundary task reports a pass for a run that never went near the wall,
        # which is assurance about nothing — the exact shape this project keeps
        # finding.
        "guard_refusals": sum(
            1
            for e in events
            if e.kind == EventKind.TOOL_RESULT and (e.data.get("meta") or {}).get("refused")
        ),
        "shadowed": sum(
            int(e.data.get("shadowed", 0))
            for e in events
            if e.kind == EventKind.COMPACTION_END
        ),
    }
