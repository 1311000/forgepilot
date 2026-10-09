"""``forgepilot --task``: the entry point that did not exist.

``run_task`` was written, tested and reachable only from a test. The demo that
first ran it end to end needed a driver script living outside the repository,
which is exactly the gap these tests are about: an example nobody else can run is
not an example.

Most of what is asserted here is *refusal*. The happy path takes a model, a
network and a checkout, so it is covered by running the example by hand; what a
unit test can hold down is that the command says no for the right reasons and
never quietly does the wrong thing instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgepilot.cli.main import main

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPO_ROOT / "configs"
EXAMPLE = REPO_ROOT / "examples" / "stat-fix"


# ---------------------------------------------------------------------------
# One command at a time
# ---------------------------------------------------------------------------
def test_two_commands_at_once_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    """`--task T-1 --chat` used to run whichever was checked first.

    Silently dropping the flag someone typed is how a ticket never runs while the
    prompt that appeared says nothing about why.
    """
    with pytest.raises(SystemExit) as caught:
        main(["--task", "BUG-1", "--chat"])

    assert caught.value.code == 2
    assert "separate commands" in capsys.readouterr().err


def test_a_command_and_the_config_dump_is_fine(capsys: pytest.CaptureFixture[str]) -> None:
    """`--dump-config` is not a mode, it is an argument to how the profile loads."""
    assert main(["--config-dir", str(CONFIGS), "--dump-config"]) == 0


# ---------------------------------------------------------------------------
# Failing before anything runs
# ---------------------------------------------------------------------------
def test_no_project_found_names_the_directory_it_looked_from(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Task mode cannot invent a project.

    Substituting the most restrictive default would strip every permission at
    once, and the symptom — an agent that refuses to write anything — reads as a
    bug in the agent rather than as a missing file.
    """
    code = main(
        [
            "--config-dir",
            str(CONFIGS),
            "--task",
            "BUG-1",
            "--token",
            "demo-token",
            "--workspace",
            str(tmp_path),
        ]
    )

    assert code == 2
    err = capsys.readouterr().err
    assert "no project.yaml" in err
    assert str(tmp_path) in err


def test_a_project_that_does_not_parse_is_reported_not_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "project.yaml"
    bad.write_text("repos: [a/b]\n", encoding="utf-8")  # not 'host/namespace/name'

    code = main(
        [
            "--config-dir",
            str(CONFIGS),
            "--task",
            "BUG-1",
            "--token",
            "demo-token",
            "--project",
            str(bad),
        ]
    )

    assert code == 2
    assert "host/namespace/name" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The example is part of the repository, so it has to keep working
# ---------------------------------------------------------------------------
def test_the_example_project_resolves_with_the_interpreter_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`project.yaml` expands `${VAR}` the same way a profile does.

    Without it the example's `test:` command would have to name a developer's
    interpreter path literally, in a file the team checks in.
    """
    from forgepilot.config.project import load_project

    monkeypatch.setenv("FORGEPILOT_PYTHON", "/somewhere/python")
    project = load_project(EXAMPLE / "project.yaml")

    assert project.test == ("/somewhere/python -m pytest -q",)
    assert project.name == "stat-fix"
    assert [r.slug for r in project.repos] == ["git.local/demo/stats"]
    # `"."` must survive into the policy. It used to be dropped silently, which
    # granted nothing and produced an agent trying eleven spellings of the path.
    assert project.to_policy().writable_paths == (".",)


def test_the_example_patch_resolves_into_a_loadable_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The example's patch is a committed file, so it can rot.

    `--dump-config` alone would not catch a broken row — it deliberately never
    imports. This applies the layers, which is what actually resolves each row's
    plugin and config.
    """
    from forgepilot.config import apply_layers, compose_layers, load_profile

    monkeypatch.setenv("FORGEPILOT_EXAMPLE_REPO", "/repos/stats")
    monkeypatch.setenv("FORGEPILOT_EXAMPLE_WORKTREES", "/worktrees")
    monkeypatch.setenv("FORGEPILOT_LLM_MODEL", "some-model")

    profile = load_profile(CONFIGS, "default")
    resolved = apply_layers(
        compose_layers(profile, [EXAMPLE / "patch.yaml"]), profile=profile.name
    )

    rows = {row.id: row for row in resolved.enabled_rows()}
    assert rows["tickets"].plugin.endswith("tickets.file:FileTicketSourcePlugin")
    assert rows["tickets"].config["root"] == "examples/stat-fix/tickets"
    assert rows["workspace"].config["repos"] == {"git.local/demo/stats": "/repos/stats"}
    assert rows["classifier"].config["model"] == "some-model"
    # A repo with no host: this is what makes the task end at READY rather than
    # claiming a pull request it never opened.
    assert rows["vcs"].plugin.endswith("vcs.local:LocalRepositoryVcsPlugin")


async def test_the_example_ticket_is_where_the_patch_says_and_can_be_run() -> None:
    """A path in a config file and a directory on disk are two different things.

    This loads the *committed* ticket through the *committed* path and puts it
    through the rules that gate task mode — so an edit that makes the example
    ticket incomplete fails here, rather than sending the example to
    AWAITING_INFO and looking like the model declined.
    """
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber
    from forgepilot.orchestration.preprocess import preprocess
    from forgepilot.seams.refs import RepoRef, TicketId

    repo = RepoRef(host="git.local", namespace="demo", name="stats")
    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.tickets.file:FileTicketSourcePlugin"),
        {"root": str(EXAMPLE / "tickets")},
    )
    try:
        ticket = await root.tickets.fetch(TicketId("BUG-1"))
        result = preprocess(ticket, known_repos=(repo,))

        assert not result.needs_human, result.reason
        assert result.repos == (repo,)
    finally:
        await dispose_fiber(fiber)
