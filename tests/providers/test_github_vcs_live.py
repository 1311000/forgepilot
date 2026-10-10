"""The one test here that touches GitHub. **It does not run by default.**

Everything else about this provider is exercised without a network: the git half
against a bare repository on disk, the HTTP half against a fake transport. What
that cannot establish is the thing most likely to be wrong — whether GitHub
actually accepts what this code sends. The API's error shapes, the `draft` flag,
the `head` ref format and the permission model are all facts about *GitHub*, and
no amount of local testing produces them.

So it is gated, and the gate is the credential:

```bash
FORGEPILOT_GITHUB_TOKEN=… FORGEPILOT_GITHUB_LIVE_REPO=1311000/test \\
  .venv/Scripts/python.exe -m pytest tests/providers/test_github_vcs_live.py -v
```

**Never paste the token into a conversation or a file in this repository.** It
goes in the environment, or in the gitignored `.env.local`.

The token needs `repo` scope on the target. It will **push a branch and open a
pull request** there, which is why the repository is named explicitly rather than
defaulted — a test that picks its own target is a test that eventually runs
against something nobody chose.
"""

from __future__ import annotations

import os

import pytest

from forgepilot.seams.refs import RepoRef
from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation
from forgepilot.seams.vcs import PullRequestDraft

TOKEN_VAR = "FORGEPILOT_GITHUB_TOKEN"
REPO_VAR = "FORGEPILOT_GITHUB_LIVE_REPO"

pytestmark = pytest.mark.skipif(
    not (os.environ.get(TOKEN_VAR) and os.environ.get(REPO_VAR)),
    reason=(
        f"live GitHub test: set {TOKEN_VAR} and {REPO_VAR} to run it. It pushes a "
        f"branch and opens a pull request on the named repository. Skipped rather "
        f"than faked — see the module docstring for what only a real host can tell you."
    ),
)


def target() -> RepoRef:
    slug = os.environ[REPO_VAR]
    parts = slug.split("/")
    assert len(parts) == 3, f"{REPO_VAR} must be 'host/namespace/name', got {slug!r}"
    return RepoRef(host=parts[0], namespace=parts[1], name=parts[2])


@pytest.fixture
async def live(tmp_path, monkeypatch):
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.vcs.github:GitHubVcsPlugin"),
        {"mirror_root": str(tmp_path / "mirrors")},
    )
    try:
        yield root.vcs
    finally:
        await dispose_fiber(fiber)


def constraints_for(repo: RepoRef) -> ExecutionConstraints:
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import Principal

    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("live"), tenant_id=TenantId("live")),
        allowed_repos=frozenset({repo}),
        permitted_operations=frozenset(
            {Operation.READ_CODE, Operation.WRITE_CODE, Operation.OPEN_PR}
        ),
        writable_paths=(".",),
        network=NetworkAccess.ALLOW,
    )


async def test_the_mirror_fetches_from_github(live) -> None:
    """`capabilities()` is a claim; this is the evidence behind it."""
    repo = target()

    path = await live.ensure_mirror(repo, constraints_for(repo))

    assert path.exists()
    assert live.capabilities() >= {"push", "pull_requests"}


async def test_a_pull_request_is_really_opened(live) -> None:
    """The end of the delivery loop, against the host that owns it.

    ⚠️ This **creates a pull request** on the target repository. It is not
    cleaned up: closing it would need another API call and another permission, and
    a leftover draft is a more honest artefact than a test that tidies up after
    itself and cannot be inspected afterwards.
    """
    repo = target()
    constraints = constraints_for(repo)
    await live.ensure_mirror(repo, constraints)

    ref = await live.open_pull_request(
        PullRequestDraft(
            repo=repo,
            source_branch="main",
            target_branch="main",
            title="ForgePilot live check — please close",
            body_md=(
                "Opened by `tests/providers/test_github_vcs_live.py` to establish that "
                "this provider talks to GitHub as documented.\n\n"
                "It is deliberately a draft. Closing it is the cleanup."
            ),
            ticket_id="LIVE-1",
        ),
        constraints,
    )

    assert ref.number > 0, "GitHub did not assign a number, so there is no PR"
    assert ref.url.startswith("https://github.com/")
