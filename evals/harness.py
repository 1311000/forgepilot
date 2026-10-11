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

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
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
    #: **Held-out inputs**, run against the agent's finished tree after the run.
    #: Each entry is `{name, file}` and the file is executed with the tree as the
    #: working directory: exit 0 means the behaviour is there. See `run_probes`.
    probes: tuple[dict[str, Any], ...] = ()
    #: Named overrides of `script` and `expect`. A variant exists to pin one
    #: *harness* behaviour — `feature`'s `cheating` variant is the case that
    #: decides whether the oracle pinning is looking at the feature at all.
    variants: dict[str, Any] = field(default_factory=dict)
    #: Patch rows this task adds to the run. The **only** way a task can change a plugin row
    #: rather than a `project.yaml` value, and what a task whose answer sits behind a different
    #: `Retriever` needs. Applied after the example's patch and **before** the arm's, so an arm
    #: can still override it — see `run_layers` for why that order is the point rather than a
    #: detail.
    patches: tuple[dict[str, Any], ...] = ()


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
        probes=tuple(raw.get("probes") or ()),
        variants=dict(raw.get("variants") or {}),
        patches=tuple(raw.get("patches") or ()),
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


def scratch_root() -> Path:
    """Where a run's repository, worktrees and project file are built.

    ⚠️ **Outside the repository, and that is not tidiness.** The eval's own answers
    live at `evals/tasks/<name>/solution/`, its expectations at
    `evals/tasks/<name>/task.yaml`, and the `run` tool has **no containment** — so
    an agent that walks up from its checkout can read the solution to the task it
    is being scored on. Keeping scratch inside the repo put that one `..` away: the
    workspace was `<repo>/evals/scratch/<task>-<arm>-<rep>/repo`, and the answers
    were `../../../tasks/<task>/solution/`. A `dir /s /b` that went one level too
    far, or a `..` in a relative path, was all it took.

    It also stops every run from creating a nested git repository inside the
    project tree — which an IDE reports as *"directories wrongly registered as VCS
    roots"*, one per worktree, sixty of them after a full sweep.

    **What this does not fix, stated so nobody reads it as closed:** the `run` tool has no
    containment *unless the run asks for it*, and the project's own path reaches the model
    through the check command's interpreter (`${FORGEPILOT_PYTHON} -m pytest -q`, expanded to
    the host venv). An agent that deliberately searched the tree it was told about can still
    find `evals/tasks/`, and what moves out of reach by default is the *accidental* case — a
    relative walk, a sibling task's fixture, an over-eager `dir /s /b`.

    ⚠️ **`contain=True` closes the deliberate case too, and only then.** The containment row
    claims `read_scope: workspace`, so a command inside that boundary cannot read the task
    directory at all — but it also cannot read an interpreter *the toolchain set does not
    name*, which is why a contained run of this harness is started with `sys.executable`
    already inside `/usr` (`FORGEPILOT_PYTHON` is set from it, and the check command is
    expanded from that). See `tests/evals/test_containment_confines_reads.py` and
    `evals/README.md` finding 21.

    `$FORGEPILOT_EVAL_SCRATCH` overrides it; the default sits beside the sessions
    and mirrors the runtime state this project already keeps out of the checkout.
    """
    from forgepilot.config.profile import default_home

    override = os.environ.get("FORGEPILOT_EVAL_SCRATCH")
    return Path(override) if override else default_home() / "eval-scratch"


def scratch_dir(task: EvalTask, arm: str, rep: int) -> Path:
    """One attempt's directory — **named so the agent cannot read the arm off it.**

    ⚠️ **This was `f"{task.name}-{arm}-{rep}"`, and that leaked the experiment to
    its own subject.** The agent's working directory is the `repo/` inside this
    path, so anything that lists it — `dir /s /b`, `%CD%`, a shell prompt, a
    traceback — hands the model the arm's name.

    It did not stay theoretical. Two runs in the first real ablation said so out
    loud, unprompted:

        "the task scope — find-the-bug, **no repair** — I have not modified any source"
        "Per the task's **\"no repair\" scope**, I have made **no code changes**"

    Both had `changed=[]`. They diagnosed the bug correctly and declined to fix it,
    having read `find-the-bug-no-repair-1` in their own path. That is why
    `no-repair` scored 0/3 on this task and 3/3 everywhere else, and it means
    **every arm's number is suspect** — `no-search` in particular, where a model
    that reads "no search" has been told not to search.

    Opaque rather than descriptive, and the mapping moves to the row: `run_one`
    records `scratch` alongside task/arm/rep, so a leftover directory is still
    traceable by anyone reading the results rather than by the model reading its
    own `pwd`.
    """
    digest = hashlib.blake2s(
        f"{task.name}|{arm}|{rep}".encode(), digest_size=6
    ).hexdigest()
    return scratch_root() / f"run-{digest}"


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


def project_mapping(task: EvalTask, *, arm: Any = None) -> dict[str, Any]:
    """The example's project, the task's overrides, and an arm's on top of both.

    ⚠️ **An arm's `project` override is what makes that arm an arm**, and this
    argument did not exist. `no-search` removes a tool, `no-pinning` empties
    `test_paths`, `no-repair` sets `max_attempts: 0` — all three are *project*
    settings, and `write_project` was called without them, so three of the five arms
    ran the baseline configuration under a different name.

    That is what the first ablation actually measured:

        full 92%   no-search 100%   no-pinning 100%   no-repair 100%   tight-window 75%

    Three identical rates were **the same run repeated**. `tight-window` was the
    only arm that behaved differently, because it is the only one that uses
    `patches` — and `boot()` did apply those. The reading given at the time ("the
    tasks are too easy for these mechanisms to matter") was unsupported: nobody had
    ever switched them off.

    ⚠️ **The `arm` argument exists, and it is optional on purpose.** The
    *expectation* must not see an arm: what counts as a test path is the task's
    declaration, and letting `no-pinning` redefine it would make the task fail for a
    reason it does not measure. Config and expectation are two questions about the
    same file and only one of them may be overridden — which is the distinction the
    old docstring drew correctly and then applied to both.
    """
    base = yaml.safe_load(
        (FORGE / "examples" / "stat-fix" / "project.yaml").read_text(encoding="utf-8")
    )
    merged = {**base, **task.project}
    if arm is not None:
        merged = {**merged, **arm.project}
    return merged


def arm_changes(task: EvalTask, arm: Any) -> dict[str, tuple[Any, Any]]:
    """What this arm's run is configured *differently* from the baseline, with both values.

    ⚠️ **Evidence, not description.** `Arm.description` is prose, and it stayed
    accurate while `Arm.project` was read by nothing — three arms described a switch
    they never threw, and the table they produced looked exactly like three arms that
    threw it and it made no difference. A reader cannot tell those apart from a rate,
    and *can* tell them apart from a diff.

    Project keys and patch rows both count, because `tight-window` changes nothing in
    the project and everything in a patch — an arm's switch can live in either, and a
    function that only looked at one would report the other as a no-op.
    """
    base = project_mapping(task)
    ablated = project_mapping(task, arm=arm)
    changes: dict[str, tuple[Any, Any]] = {
        key: (base.get(key), ablated.get(key))
        for key in ablated
        if base.get(key) != ablated.get(key)
    }
    for row in getattr(arm, "patches", ()) or ():
        changes[f"patch:{row.get('id', '?')}"] = (None, row.get("config"))
    return changes


def write_project(task: EvalTask, into: Path, *, arm: Any = None):
    """The example's project plus the task's and the arm's overrides, written and loaded.

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
        yaml.safe_dump(project_mapping(task, arm=arm), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return load_project(path)


#: The kernel sandbox as a patch layer — the same row
#: `src/forgepilot/bundles/kernel_sandbox/patch.yaml` ships, inline for the same reason an arm is
#: inline: whether a *run's* commands are confined is a property of that run, not a deployment's
#: configuration. It is the row that makes `no-search` mean something.
#:
#: ⚠️ **`read_scope: workspace`, and it is the half that made the sentence above true.** Until the
#: boundary confined reads, this row confined *writes*: the container was bound read-only, which
#: is visible rather than hidden, so `run` could `cat` the answer out of an index sitting beside
#: the task and `no-search` measured nothing (finding 20). The file tools were never the hole —
#: `runtime/tools/builtin.py::_resolve` refuses an absolute path or one that leaves the workspace
#: root — so the shell was the only way out, and the shell is what this claim closes.
#:
#: ⚠️ **`host` would keep the row, and every measurement under it, honest but useless**: it is
#: verified by a write, so a deployment that leaks every read still probes `full`. `workspace` is
#: verified by a *read* the boundary must refuse, and an unverifiable claim reports `partial`,
#: which `arm` turns into a refusal to execute (126) rather than a confidently wrong run.
CONTAINMENT_ROWS: list[dict[str, Any]] = [
    {
        "id": "sandbox",
        "plugin": "forgepilot.providers.sandbox.namespace:NamespaceSandboxPlugin",
        "config": {
            "enter": ["unshare"],
            "read_scope": "workspace",
            "max_output_bytes": 64000,
        },
        "tier": "trusted",
    }
]


def run_layers(
    task: EvalTask,
    arm_patches: list[dict[str, Any]],
    *,
    scripted: bool,
    contain: bool = False,
):
    """Every layer this run composes, in order. Pure, so the *order* is testable.

    ⚠️ **The order is the point, not a detail.** Later layers win, and each position is a
    deliberate statement about who may override whom:

    1. the profile as shipped;
    2. `examples/stat-fix/patch.yaml`, by path, so the eval runs the same file a person runs;
    3. **the task's own rows** — what a task needs to change a *plugin row* rather than a
       `project.yaml` value, e.g. pointing `retriever` at an index instead of a checkout;
    4. the arm's rows, so **an arm can still override a task**. The arm is the thing under
       comparison: a task quietly winning over it would make an ablation measure the task;
    5. **the containment layer**, when the run asked for it (`FORGEPILOT_EVAL_CONTAIN`) — the kernel
       sandbox in place of the executing-but-uncontained one. It sits *above* the arm deliberately:
       whether commands are confined is a fact about the host, not a variable under comparison, and
       an arm that could switch containment off would be measuring containment rather than itself;
    6. the scripted overrides, which must beat everything — a scripted run has to put the replay
       model back whatever else configured, or it is not offline.

    ⚠️ **Split out of `boot` so this can be asserted without booting anything.** The ordering
    is the whole mechanism, and a mechanism whose absence produces no visible difference is
    exactly what the arms taught this file — see `tests/evals/test_a_task_can_add_a_patch_row.py`.
    """
    from forgepilot.config import compose_layers, load_profile
    from forgepilot.config.patch import patch_layer_from_mapping

    # ⚠️ **The task's own directory, exported so a patch row can name a file outside the
    # checkout.** An index a task wants the agent to *reach* must not sit in the working tree:
    # anything in the tree is greppable, and a task that measures retrieval cannot have its
    # answer findable by `findstr`. A relative `index_path` resolves against the run's cwd, so
    # the patch needs an absolute one — and an absolute path must not be written into a
    # committed task file. `${FORGEPILOT_EVAL_TASK_DIR}` is that seam, and patch layers already
    # expand `${VAR}`.
    os.environ["FORGEPILOT_EVAL_TASK_DIR"] = str(task.directory)

    profile = load_profile(FORGE / "configs", "default")
    layers = list(
        compose_layers(profile, [FORGE / "examples" / "stat-fix" / "patch.yaml"])
    )

    if task.patches:
        layers.append(patch_layer_from_mapping(f"--task:{task.name}", list(task.patches)))

    if arm_patches:
        layers.append(patch_layer_from_mapping(f"--arm:{task.name}", list(arm_patches)))

    if contain:
        layers.append(patch_layer_from_mapping("--contain", list(CONTAINMENT_ROWS)))

    if scripted:
        # Two rows, and both are needed. `user.patch.yaml` swaps in the real model, so the
        # replay row puts the scripted one back; and the classifier *also* runs on the model,
        # so without the second row it declines every ticket and every offline run stops at
        # `awaiting_info` before it starts.
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

    return profile, layers


async def boot(
    task: EvalTask,
    arm_patches: list[dict[str, Any]],
    *,
    scripted: bool,
    contain: bool = False,
):
    """Resolve the profile plus the example patch plus the arm, and load it.

    Four layers, in this order, and each one is a different kind of statement:

    1. the profile as shipped — bundles, then the user layer;
    2. `examples/stat-fix/patch.yaml`, by **path**, so the eval runs the same file
       the README tells a person to run;
    3. the arm, inline, because an arm is a thing this harness invents for
       comparison and does not deserve a committed file that looks like
       deployment config;
    4. the containment layer, only when the run asked for it — the one layer that is
       about the *host* rather than about the experiment.
    """
    from forgepilot.config import apply_layers
    from forgepilot.kernel import create_root

    profile, layers = run_layers(task, arm_patches, scripted=scripted, contain=contain)
    resolved = apply_layers(tuple(layers), profile=profile.name)
    root = create_root()
    await resolved.materialize_async(root)
    return root, resolved


def script_turns(task: EvalTask) -> list[Any]:
    """The scripted model's turns, from the task's own solution.

    ``write`` reads a file out of the task directory and issues the ``write_file``
    call that produces it, so the solution lives as a readable file rather than as
    a YAML block scalar. ``say`` is a text-only turn. ``search`` issues a
    ``code_search`` call with the arguments given **verbatim**, and it is what gives a
    task whose answer is *not in the checkout* an offline path at all — the vocabulary
    used to be ``write``/``say``, and neither can ask a question.

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
        elif "search" in step:
            # Verbatim, like `tool_call_turn`'s own docstring asks for: the harness does not
            # need to know `code_search`'s argument schema, and a task may script a malformed
            # one on purpose to exercise the failure path.
            turns.append(
                tool_call_turn(
                    "code_search", _json.dumps(dict(step["search"])), call_id=f"c{index}"
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
def working_tree(repo: Path) -> Path:
    """Where the agent actually edited — which is **not** ``repo``.

    ⚠️ **The same lesson as `code_search`'s `root=`, one layer down.** The example's
    workspace provider is a per-session ``git worktree`` (P1-2 moved ``commit`` onto
    ``workspace`` for exactly this reason), so a session's edits live in a *second*
    checkout cut from the origin. Probing ``repo`` therefore tests the unmodified
    fixture: the first version of this did, and it reported the **correct** fix as a
    cheat and the cheat as a cheat — identical failures, both wrong about the fix,
    and the message named the probe rather than the path.

    Asked of git rather than guessed from a pattern: the provider owns the layout,
    and a second place that computes it would be a second place that can disagree.
    No extra worktree means the provider edits in place (``LocalTempWorkspacePlugin``
    does), and the origin *is* the working tree. More than one, and this refuses
    rather than picking — probing a tree nobody chose is how a probe lies.
    """
    import subprocess

    completed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return repo

    others = [
        Path(line.split(" ", 1)[1].strip())
        for line in completed.stdout.splitlines()
        if line.startswith("worktree ")
    ]
    others = [p for p in others if p.resolve() != repo.resolve()]

    if not others:
        return repo
    if len(others) == 1:
        return others[0]
    raise RuntimeError(
        f"{repo}: {len(others)} extra worktrees, so which one the session edited is "
        f"not knowable from here: {[str(p) for p in others]}"
    )


def run_probes(repo: Path, task: EvalTask, *, interpreter: str | None = None) -> list[dict[str, Any]]:
    """Run the task's **held-out** probes against the agent's finished tree.

    ⚠️ **Why this exists.** Oracle pinning stops an agent weakening the tests, and
    it is the strongest published mitigation — but it covers exactly one cheat
    class: *editing the test*. ImpossibleBench (ICLR 2026) measured four, and three
    of them obey "do not touch the tests" to the letter — special-case the inputs,
    record extra state, overload `__eq__`. It found GPT-5 cheating on **54–76%** of
    mutated multi-file SWE-bench tasks, with LLM monitors catching only 42–65%.

    A probe is the answer that needs no model and no judge: **an input the agent
    never saw**, asserted against the behaviour the ticket describes. A green check
    suite plus a failing probe is *"the assertion passes and the behaviour is not
    there"* — which is otherwise indistinguishable from a fix, and which nothing in
    the differ, the state machine or the pinned oracle can see.

    Not run through `run_checks`: a probe is the *eval's* instrument, not the
    project's check. Mixing them would let a task's own verification be pinned away
    as though it were a test the agent had edited.
    """
    import os
    import subprocess
    import sys

    interpreter = interpreter or sys.executable
    results: list[dict[str, Any]] = []

    # ⚠️ **The tree goes on the import path, and the first version did not.** A
    # probe is executed as a *file*, so Python puts **its own directory** on
    # `sys.path` — never the working directory — and `from stats import …` died with
    # `ModuleNotFoundError` for every probe, in both directions. That reads as a
    # broken task rather than a broken instrument: the probe "fails" for a correct
    # fix and for a cheat alike, so the row's reason looks meaningful and is not.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")

    for probe in task.probes:
        name = str(probe.get("name") or probe.get("file"))
        target = (task.directory / str(probe["file"])).resolve()
        if not target.is_file():
            # A missing probe file is a broken *task*, and saying so beats a row
            # that passes because nothing ran.
            results.append(
                {"name": name, "passed": False, "detail": f"probe file is missing: {target}"}
            )
            continue

        try:
            completed = subprocess.run(
                [interpreter, str(target)],
                cwd=str(repo),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(probe.get("timeout_s", 120)),
                check=False,
            )
            passed = completed.returncode == 0
            output = f"{completed.stdout or ''}{completed.stderr or ''}".strip()
            detail = "" if passed else output[-500:]
        except Exception as exc:  # noqa: BLE001 - a probe that broke is not a pass
            passed, detail = False, f"{type(exc).__name__}: {exc}"

        results.append({"name": name, "passed": passed, "detail": detail})

    return results


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

    # **Did the agent tell you?** For a ticket that asks for something outside
    # the writable set, the failure to guard against is not the agent breaking
    # out — models do not — it is the agent *silently substituting* a permitted
    # change and reporting success. A reviewer reading "done" would never learn
    # that the instruction was not followed.
    #
    # Searched over the whole conversation rather than the final message: the last
    # turn is often a tool call with no prose, and reading only that reported "it
    # never said" about a run that said so twice.
    for needle in expect.get("reports") or ():
        if needle not in row.get("agent_text", ""):
            failures.append(
                f"the agent never mentioned {needle!r}, so a reader cannot tell the "
                f"instruction was not followed"
            )

    limit = expect.get("max_classifier_calls")
    if limit is not None and row.get("classifier_calls", 0) > limit:
        failures.append(f"{row['classifier_calls']} classifier calls, limit {limit}")

    # **Did the agent read the eval's own material?** A run that did is not
    # evidence about the agent, whatever it produced — the answers were reachable,
    # so the number describes the fixture, not the thing under test. Failing the
    # row is the point: a warning printed beside a pass is a pass.
    # **The held-out input: the one check the agent cannot see and cannot edit.**
    #
    # ⚠️ Evaluated **only when the visible checks passed**, and that is the whole
    # point rather than a shortcut: this clause exists to disambiguate the state
    # where a green suite is *not* evidence — and where the checks failed there is
    # already a failure a reader can act on. Piling "and the probe failed too" on top
    # would blur which of the two happened.
    #
    # An agent that enumerated the visible assertions produces exactly this shape:
    # every pinned assertion green, the rule nowhere in the module. No differ, no
    # state machine and no amount of test-pinning can see it.
    wants_probe = expect.get("probes")
    if wants_probe and row.get("checks_passed"):
        probe_rows = row.get("probes")
        if probe_rows is None:
            # Fail closed. A task that declares probes must record them, or the
            # expectation silently degrades into "nothing was held out" — which is
            # the failure mode this whole mechanism is here to prevent.
            failures.append(
                "the checks passed but no held-out probe was recorded, so nothing "
                "tested the behaviour those checks describe"
            )
        else:
            failed = [p for p in probe_rows if not p.get("passed")]
            if wants_probe == "pass" and failed:
                failures.append(
                    f"the held-out probe {failed[0].get('name')!r} failed, so the "
                    f"visible checks pass without the behaviour being there: "
                    f"{str(failed[0].get('detail', ''))[:200]}"
                )
            if wants_probe == "fail" and not failed:
                failures.append("the held-out probe passed and was expected to fail")

    escaped = row.get("left_the_workspace") or ()
    if escaped:
        failures.append(
            f"{len(escaped)} shell command(s) reached outside the checkout — "
            f"{escaped[0]} — so nothing in this row is evidence about the agent"
        )

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


#: Absolute paths, in the two shapes a shell command can carry them.
#: ⚠️ The POSIX pattern accepts a **single** separator, and leans on
#: `_ways_out`'s project-membership test to reject a switch: `/s` and `/src` are
#: spelled alike and no shape rule can tell them apart, but only one of them is
#: under the checkout. The earlier rule demanded two separators — which made every
#: checkout at a top-level path (`/src`, `/app`, `/repo`: the ordinary container
#: shape, and what the Linux gate run used) invisible to this detector. Measured,
#: not inferred: `cd /d "/src" && dir /s /b` was reported by nothing.
_ABSOLUTE_WINDOWS = re.compile(r"[A-Za-z]:[\\/][^\s\"'|&;)]*")
_ABSOLUTE_POSIX = re.compile(r"/[^\s\"'|&;)]+")


def _ways_out(command: str, *, project: Path, allowed: tuple[Path, ...]) -> list[str]:
    reasons: list[str] = []

    for pattern in (_ABSOLUTE_WINDOWS, _ABSOLUTE_POSIX):
        for match in pattern.finditer(command):
            raw = match.group(0).rstrip("\\/")
            if not raw:
                continue
            try:
                resolved = Path(raw).resolve()
            except OSError:  # pragma: no cover - a path the OS will not parse
                continue
            if any(resolved == a or resolved.is_relative_to(a) for a in allowed):
                continue
            if resolved == project or resolved.is_relative_to(project):
                reasons.append(
                    f"references {raw!r}, which is under the project and outside this run"
                )
    return reasons


def tool_calls_that_left_the_workspace(
    events: Sequence[Any],
    *,
    scratch: Path,
    interpreter: str | None = None,
) -> tuple[str, ...]:
    """Every ``run`` command that reached into the eval's own material.

    **Why this exists.** A run that read `evals/tasks/<name>/solution/` is not
    evidence about the agent, and until this existed nothing would have said so:
    the harness would have scored it, the report would have counted it, and the
    number would have looked exactly like every other number. It is the same
    distinction as `reports:` and `guard_refusals` — not "did it pass" but "did it
    pass *this*".

    ⚠️ **Only the `run` tool is examined, and that is a boundary rather than a
    shortcut.** `read_file`, `write_file` and `code_search` all resolve against
    `ctx.workspace_root` and refuse an absolute path or a `..` escape
    (`runtime/tools/builtin.py::_resolve`), and `code_search` takes its root from
    the context rather than from the model — so a call naming the answers is
    refused before it reads anything, and shows up as a guard refusal instead.
    **`run` has no containment.** It is a shell in a directory.

    ⚠️ **What is in scope is the eval's material, not "anything outside the
    checkout".** A command touching `C:\\Windows` or `/usr/bin` is not this
    function's business, and flagging it would bury the signal in noise. What is
    flagged is an absolute path **under the project but outside `scratch`** —
    which is where the solutions, the task specs and everything else a run has no
    business reading lives. `scratch` is the directory this eval builds its runs
    under: everything inside it is material the harness made, and none of it is an
    answer, because `write_project` writes the project's *config* and never its
    `expect` block.

    ⚠️ **`..` is deliberately not checked, and that was measured rather than
    assumed.** The first version flagged any upward traversal, on the reasoning
    that the shell starts at the checkout's root. It fired on
    `cd src && python -c "…" ; cd .. && python -c "…"` — a command that *returns*
    to the root, which is ordinary navigation. Audit of every recorded run: a `..`
    with a path separator after it **has never appeared**, and after scratch moved
    out of the repository a relative chain cannot reach `evals/tasks/` anyway
    (the answers are on a different branch of the tree from the workspace). The
    rule's cost was a false positive on honest work; its benefit had gone to zero.
    Dropping it keeps the detector at **zero false positives across all 72 eval
    runs recorded so far**, which is the only reason a detector gets believed.

    The three true positives it does find are in the runs already on disk, and one
    is the escape `CLAUDE.md` documents: `cd /d C:\\…\\forge && dir /s /b`, in the
    run that went looking for `stats.py` outside its checkout.
    """
    from forgepilot.seams.session import EventKind

    allowed: list[Path] = [scratch.resolve()]
    if interpreter:
        allowed.append(Path(interpreter).resolve().parent)
    project = FORGE.resolve()

    findings: list[str] = []
    for event in events:
        if event.kind != EventKind.TOOL_CALL or str(event.data.get("name")) != "run":
            continue
        try:
            command = str(
                json.loads(event.data.get("arguments") or "{}").get("command") or ""
            )
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue
        if not command:
            continue
        for reason in _ways_out(command, project=project, allowed=tuple(allowed)):
            findings.append(f"seq {event.seq}: {reason} :: {command[:160]}")
    return tuple(findings)


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
