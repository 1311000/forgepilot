"""The two-repository GitHub deployment, checked without a network.

Three things this file exists to catch, and none of them is expressible as an eval
task:

1. **The ticket resolves to both repositories, and to nothing else.** `preprocess`
   scans the description against `project.repos`, so a slug the project does not
   declare is a repository nobody authorised, and a project that declares one the
   ticket never names is a delivery that silently does not happen.
2. **The three repository → path rows agree.** `vcs` derives the mirror path as
   `<mirror_root>/<host>/<namespace>/<name>`, `workspace` cuts its worktree from
   that directory, and `retriever` names the slug for enumeration. Nothing in the
   composition checks that they say the same thing — the example's own README says
   so — and the failure mode is `repo '…' is not permitted` at the point the second
   delivery starts, which is the far end of a live run.
3. **The principal may open a pull request on either repository.** The grant is
   intersected with the project, so a prefix that covers one of the two leaves the
   other at READY with a commit and no pull request.

The live run itself — two pushed branches and two pull requests — is
`README.md`'s procedure, not a test: it needs a credential and it writes to a real
host.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml

from forgepilot.config.project import load_project
from forgepilot.orchestration.preprocess import preprocess
from forgepilot.providers.tickets.file import _ticket_from

DEPLOYMENT = Path(__file__).resolve().parents[2] / "examples" / "stat-fix" / "two-repos" / "github"
SLUGS = ("github.com/1311000/test", "github.com/1311000/test-reporting")


@pytest.fixture()
def project(monkeypatch: pytest.MonkeyPatch):
    # The project's `test:` line is `${FORGEPILOT_PYTHON} -m pytest -q`, and a
    # `${VAR}` with no default is refused when the variable is unset - so the
    # fixture supplies one rather than making the test depend on the shell.
    monkeypatch.setenv("FORGEPILOT_PYTHON", "python")
    return load_project(DEPLOYMENT / "project.yaml")


@pytest.fixture()
def patch_rows() -> list[dict]:
    return yaml.safe_load((DEPLOYMENT / "patch.yaml").read_text(encoding="utf-8"))


def _row(rows: list[dict], row_id: str) -> dict:
    for row in rows:
        if row.get("id") == row_id:
            return row
    raise AssertionError(f"no `{row_id}` row in patch.yaml")


def test_the_project_knows_exactly_the_two_host_repositories(project) -> None:
    assert tuple(repo.slug for repo in project.repos) == SLUGS


def test_the_ticket_resolves_to_both_repositories_and_no_others(project) -> None:
    source = DEPLOYMENT / "tickets" / "BUG-2.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    ticket = _ticket_from(raw, fallback_id=source.stem, source=source)

    result = preprocess(ticket, known_repos=project.repos)

    assert result.ready, result.missing
    assert tuple(repo.slug for repo in result.repos) == SLUGS


def test_the_three_repository_rows_cover_every_project_slug(project, patch_rows) -> None:
    slugs = set(SLUGS)
    workspace = _row(patch_rows, "workspace")["config"]["repos"]
    retriever = _row(patch_rows, "retriever")["config"]["roots"]

    assert set(workspace) == slugs
    assert set(retriever) == slugs
    # The retriever's map is not "where to search" - it names the slugs. Pointing
    # it somewhere else is harmless; pointing it at the *wrong slug* is not.
    assert retriever == workspace, "the retriever and workspace rows must name the same paths"

    mirrors = _row(patch_rows, "vcs")["config"]["mirror_root"]
    assert isinstance(mirrors, str) and mirrors
    for slug, path in workspace.items():
        assert path == f"{mirrors}/{slug}", f"{slug} is not under the vcs mirror root"


def test_the_principal_may_open_a_pull_request_on_either_repository(patch_rows) -> None:
    principal = _row(patch_rows, "principal")["config"]["tokens"][0]

    assert "open_pr" in principal["operations"]
    for slug in SLUGS:
        assert any(
            fnmatch(slug, f"{prefix.rstrip('*')}*") for prefix in principal["repo_prefixes"]
        ), f"no repo_prefix covers {slug}"


def test_the_example_turns_the_ask_off_on_purpose(patch_rows) -> None:
    # It has no answerer: leaving `open_pr` in `ask_operations` would make the ask
    # resolve to UNAVAILABLE and both deliveries stop at READY - which is the other
    # half of the demonstration, and a different example.
    assert _row(patch_rows, "policy")["config"]["ask_operations"] == []
