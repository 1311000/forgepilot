"""The cross-repository check: the only one that can see the combination.

A repository's `test:` runs inside that repository's tree. A change that spans two
repositories and only breaks in combination therefore earns two passes for a
combination nobody executed. These tests pin the step that closes that gap — and,
just as importantly, the states in which it **refuses to pretend** it ran.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from forgepilot.config.project import ProjectConfig, project_from_mapping
from forgepilot.orchestration.modes import task as task_module
from forgepilot.orchestration.modes.task import (
    RepoDelivery,
    _run_merged_checks,
    _task_verdict,
)
from forgepilot.orchestration.task_state import TaskState
from forgepilot.providers.sandbox.scripted import ScriptedResult
from forgepilot.runtime.repair import CheckResult
from forgepilot.seams.refs import RepoRef
from forgepilot.seams.security import NetworkAccess, Operation
from tests.conftest import constraints_for

A = RepoRef(host="git.corp", namespace="team", name="stats")
B = RepoRef(host="git.corp", namespace="team", name="reporting")


class FakeSandbox:
    """Answers every command the same way, and remembers where it ran."""

    def __init__(self, *, exit_code: int = 0, stdout: str = "1 passed") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.cwds: list[Path] = []

    async def run(self, argv, *, policy, cwd, env, timeout_s):  # noqa: ANN001
        from forgepilot.seams.sandbox import SandboxEnforcement

        self.cwds.append(Path(cwd))

        class Outcome:
            executed = True
            enforcement = SandboxEnforcement.FULL
            timed_out = False
            exit_code = self.exit_code
            stdout = self.stdout
            stderr = ""

        return Outcome()


class FakeSession:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def append(self, kind, data, **kwargs):  # noqa: ANN001
        self.events.append((str(kind), dict(data)))


class FakeWorkspace:
    """Copies the trees it is handed, the way the real provider does."""

    def __init__(self, *, supported: bool = True) -> None:
        self.supported = supported
        self.trees: list[tuple[RepoRef, Path]] = []
        self.disposed: list[Path] = []

    async def merged_checkout(self, trees, *, session_id, name):  # noqa: ANN001
        self.trees = list(trees)
        root = Path(trees[0][1]).parent / f"merged-{name}"
        root.mkdir(parents=True, exist_ok=True)
        for repo, tree in trees:
            (root / repo.name).mkdir(exist_ok=True)
            (root / repo.name / "same.txt").write_text(str(tree), encoding="utf-8")
        return root

    def dispose_merged(self, root):  # noqa: ANN001
        self.disposed.append(Path(root))


def a_delivery(repo: RepoRef, root: Path, *, state=TaskState.DELIVERED) -> RepoDelivery:
    return RepoDelivery(repo=repo, state=state, root=str(root))


def a_project(**extra: Any) -> ProjectConfig:
    return project_from_mapping(
        {
            "name": "two-repos",
            "repos": [A.slug, B.slug],
            "allowed_operations": ["read_code", "write_code", "run_tests"],
            "merged_checks": ["python -m pytest -q tests/integration"],
        }
        | extra
    )


@pytest.fixture
def two_trees(tmp_path: Path) -> list[tuple[RepoRef, Path]]:
    trees = []
    for repo in (A, B):
        tree = tmp_path / repo.name
        tree.mkdir()
        (tree / "CODE.py").write_text(repo.name, encoding="utf-8")
        trees.append((repo, tree))
    return trees


async def test_a_merged_check_runs_in_one_tree_holding_every_repository(two_trees) -> None:
    workspace = FakeWorkspace()
    sandbox = FakeSandbox()
    session = FakeSession()
    deliveries = [a_delivery(repo, tree) for repo, tree in two_trees]

    result = await _run_merged_checks(
        project=a_project(),
        deliveries=deliveries,
        workspace=workspace,
        sandbox=sandbox,
        session=session,
        session_id="task-session",
        constraints=constraints_for(Operation.RUN_TESTS),
    )

    assert result is not None
    assert result.passed
    # Both repositories were handed over, in delivery order, from the trees the
    # per-repository checks already looked at.
    assert workspace.trees == two_trees
    # The command ran in the merged tree, not in either repository.
    merged_root = sandbox.cwds[0]
    assert merged_root.name.startswith("merged-")
    assert (merged_root / A.name / "same.txt").is_file()
    assert (merged_root / B.name / "same.txt").is_file()
    # And it says on the event that this was the merged run.
    assert result.meta["label"] == "merged"
    kind, data = session.events[-1]
    assert kind == "task/check"
    assert data["repo"] is None
    assert data["meta"]["label"] == "merged"
    # The merged tree is cleaned up: it is a copy, and leaving it around makes the
    # next run's copy look current.
    assert workspace.disposed == [merged_root]


async def test_one_repository_cannot_run_a_merged_check(two_trees) -> None:
    workspace = FakeWorkspace()
    session = FakeSession()

    result = await _run_merged_checks(
        project=a_project(),
        deliveries=[a_delivery(*two_trees[0])],
        workspace=workspace,
        sandbox=FakeSandbox(),
        session=session,
        session_id="task-session",
        constraints=constraints_for(Operation.RUN_TESTS),
    )

    assert result is not None
    assert result.could_not_run
    assert "at least two repositories" in result.reason
    # Nothing was merged and nothing was executed.
    assert workspace.trees == []
    assert session.events[-1][1]["couldNotRun"] is True


async def test_a_failed_delivery_skips_the_merged_check(two_trees) -> None:
    session = FakeSession()
    deliveries = [
        a_delivery(*two_trees[0], state=TaskState.FAILED),
        a_delivery(*two_trees[1]),
    ]

    result = await _run_merged_checks(
        project=a_project(),
        deliveries=deliveries,
        workspace=FakeWorkspace(),
        sandbox=FakeSandbox(),
        session=session,
        session_id="task-session",
        constraints=constraints_for(Operation.RUN_TESTS),
    )

    assert result is not None
    assert result.could_not_run
    # It names the repository, because "the combination was not checked" is not
    # actionable without knowing which half never landed.
    assert A.slug in result.reason


async def test_a_workspace_that_cannot_merge_says_which_provider_can(two_trees) -> None:
    result = await _run_merged_checks(
        project=a_project(),
        deliveries=[a_delivery(repo, tree) for repo, tree in two_trees],
        workspace=object(),
        sandbox=FakeSandbox(),
        session=FakeSession(),
        session_id="task-session",
        constraints=constraints_for(Operation.RUN_TESTS),
    )

    assert result is not None
    assert result.could_not_run
    # The refusal names the provider to use: a config error whose fix is not
    # written down is a config error that stays.
    assert "MergedCheckoutWorkspacePlugin" in result.reason


async def test_a_failed_merged_check_fails_the_task_after_publishing(task_rig, monkeypatch) -> None:
    from tests.task.test_task_mode import FIXED, make_plan, run, text_turn, write_fix

    plan = await make_plan(task_rig)
    assert plan.ready, plan.describe()

    async def failing(**kwargs: Any):
        return CheckResult(
            passed=False,
            commands=("python -m pytest -q tests/integration",),
            command="python -m pytest -q tests/integration",
            output="ImportError: cannot import name 'clamp' from 'stats'",
            exit_code=1,
            reason="the checks failed",
            meta={"label": "merged"},
        )

    monkeypatch.setattr(task_module, "_run_merged_checks", failing)

    task_rig.llm.script(write_fix(FIXED), text_turn("bounded the retry loop"))
    task_rig.sandbox.script(ScriptedResult(exit_code=0))

    outcome = await run(task_rig, plan)

    # The per-repository work was published — the reason has to say so, or the
    # reader thinks nothing exists.
    assert outcome.for_repo(task_rig.project.repos[0].slug).delivered
    assert outcome.state is TaskState.FAILED
    assert "merged checks failed" in outcome.reason
    assert "pull requests exist" in outcome.reason
    # And the verdict rule still reads the deliveries: it did not decide this.
    assert _task_verdict(outcome.deliveries)[0] is TaskState.DELIVERED


CHECK_COMMAND = (
    "python -c \"import sys; sys.path[:0] = ['stats', 'reporting']; "
    "import stats, windows; "
    'assert stats.WINDOW == windows.WINDOW, (stats.WINDOW, windows.WINDOW)"'
)


async def test_the_check_catches_a_combination_that_each_tree_passes(
    tmp_path: Path,
) -> None:
    """The falsifier: two repositories that are each fine and disagree together.

    The command really runs (the local sandbox executes), in the merged tree, and
    the only thing that changes between the two calls is one number in the second
    repository. A per-repository check cannot see this: neither tree contains both
    files.
    """
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    stats = tmp_path / "stats"
    stats.mkdir()
    (stats / "stats.py").write_text("WINDOW = 3\n", encoding="utf-8")
    reporting = tmp_path / "reporting"
    reporting.mkdir()
    (reporting / "windows.py").write_text("WINDOW = 3\n", encoding="utf-8")

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.workspace.merged:MergedCheckoutWorkspacePlugin"),
        {"repos": {}, "worktree_root": str(tmp_path / "worktrees")},
    )
    sandbox_fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.sandbox.local:LocalProcessSandboxPlugin"), {}
    )
    try:
        session = FakeSession()
        # `network: allow` because the local backend reports NONE enforcement and
        # refuses to pretend otherwise; this test runs real commands on purpose.
        constraints = replace(constraints_for(Operation.RUN_TESTS), network=NetworkAccess.ALLOW)
        project = project_from_mapping(
            {
                "name": "two",
                "repos": [A.slug, B.slug],
                "merged_checks": [CHECK_COMMAND],
            }
        )
        deliveries = [
            a_delivery(A, stats),
            a_delivery(B, reporting),
        ]

        passing = await _run_merged_checks(
            project=project,
            deliveries=deliveries,
            workspace=root.workspace,
            sandbox=root.sandbox,
            session=session,
            session_id="s-1",
            constraints=constraints,
        )
        assert passing is not None
        assert passing.passed, passing.output

        # One repository changes its mind. Each tree is still importable and every
        # per-repository check would still pass; the pair is what fails.
        (reporting / "windows.py").write_text("WINDOW = 2\n", encoding="utf-8")

        failing = await _run_merged_checks(
            project=project,
            deliveries=deliveries,
            workspace=root.workspace,
            sandbox=root.sandbox,
            session=session,
            session_id="s-1",
            constraints=constraints,
        )
        assert failing is not None
        assert not failing.passed
        assert "AssertionError" in failing.output
        assert "(3, 2)" in failing.output
    finally:
        await dispose_fiber(sandbox_fiber)
        await dispose_fiber(fiber)
