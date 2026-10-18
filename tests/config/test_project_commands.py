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
