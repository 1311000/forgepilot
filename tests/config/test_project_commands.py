"""What `project.yaml` declared, kept beside what it expands to.

A `test:` line is a machine's business — `${FORGEPILOT_PYTHON}` exists because the
interpreter that has pytest differs per developer. Expansion is how the check runs;
it is not how the project described itself, and the pull request body quotes the
latter. This file pins the difference at the loader, which is where it can be lost.
"""

from __future__ import annotations

from pathlib import Path

from forgepilot.config.project import load_project


def test_a_test_command_keeps_its_declared_form_beside_the_expanded_one(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FORGEPILOT_PYTHON", r"D:\somewhere\.venv\Scripts\python.exe")
    (tmp_path / "project.yaml").write_text(
        "name: svc\n"
        "test: ['${FORGEPILOT_PYTHON} -m pytest -q']\n"
        "test_paths: ['tests']\n",
        encoding="utf-8",
    )

    project = load_project(tmp_path)

    assert project.test == (r"D:\somewhere\.venv\Scripts\python.exe -m pytest -q",)
    assert project.commands_declared["test"] == ("${FORGEPILOT_PYTHON} -m pytest -q",), (
        "the declaration is what a pull request body quotes; losing it here means the "
        "only surviving form names one machine's interpreter"
    )


def test_a_project_without_checks_declares_nothing(tmp_path: Path) -> None:
    (tmp_path / "project.yaml").write_text("name: svc\ntest_paths: ['tests']\n", encoding="utf-8")

    project = load_project(tmp_path)

    assert project.test == ()
    assert project.commands_declared == {}


def test_merged_checks_are_read_and_default_to_none(tmp_path: Path) -> None:
    """Cross-repository checks are their own key, not a `test:` convention.

    They cannot be derived from `test:`: whether a project has a check that needs
    every repository in one directory is a fact only the project knows, and a
    harness that guessed it would run a single repository's suite in a tree
    without its neighbours and call the result a pass.
    """
    (tmp_path / "project.yaml").write_text(
        "name: two-repos\n"
        "repos: [git.corp/team/stats, git.corp/team/reporting]\n"
        "test: ['python -m pytest -q']\n"
        "merged_checks:\n"
        "  - 'python -m pytest -q tests/integration'\n",
        encoding="utf-8",
    )

    project = load_project(tmp_path)

    assert project.merged_checks == ("python -m pytest -q tests/integration",)
    # Merged checks are lines the harness runs, like `test:`; they are not a
    # permission a model's `run` call may use, so they stay out of `commands`.
    assert project.commands is None

    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "project.yaml").write_text("name: single\n", encoding="utf-8")
    assert load_project(tmp_path / "other").merged_checks == ()
