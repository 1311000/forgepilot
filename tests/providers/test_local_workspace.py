"""The temp-directory workspace, and the one thing it must not pretend to be.

It is a directory of seeded files — its own docstring says "no clone is
performed". That is fine for the tool and sandbox tests that only need somewhere
to read and write. It is *not* a repository, and now that ``commit`` lives on the
``Workspace`` seam, a provider that cannot commit has to refuse rather than hand
back a sha it made up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgepilot.seams.refs import CommitAuthor, RepoRef, SessionId

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")


@pytest.fixture
async def workspace():
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root, dispose_fiber

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.workspace.local:LocalTempWorkspacePlugin"),
        {"seed": {"src/app.py": "def main():\n    return 1\n"}},
    )
    try:
        yield root.workspace
    finally:
        await dispose_fiber(fiber)


@pytest.fixture
def writer():
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import (
        ExecutionConstraints,
        NetworkAccess,
        Operation,
        Principal,
    )

    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset({REPO}),
        permitted_operations=frozenset({Operation.READ_CODE, Operation.WRITE_CODE}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


async def test_it_still_does_the_directory_job(workspace, tmp_path: Path) -> None:
    """The refusal below must not make this provider useless for what it is for."""
    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))
    (handle.root / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")

    assert (handle.root / "src" / "app.py").exists()
    assert "src/new.py" in await workspace.diff(handle) or "new.py" in " ".join(
        await workspace.changed_paths(handle)
    )


async def test_a_commit_is_refused_rather_than_invented(workspace, writer) -> None:
    """No repository, no commit — and no fabricated sha.

    A made-up sha is the worst of the options: it looks like delivery, it appears
    in the session log as evidence, and nothing downstream can tell that no commit
    exists.
    """
    from forgepilot.seams.workspace import NotARepository

    handle = await workspace.prepare(REPO, "main", SessionId("s-1"))

    with pytest.raises(NotARepository, match="not a repository"):
        await workspace.commit(
            handle, "fix: something", CommitAuthor(name="n", email="n@e.test"), writer
        )
