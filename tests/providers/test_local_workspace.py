"""The temp-directory workspace, and the one thing it must not pretend to be.

It is a directory of seeded files — its own docstring says "no clone is
performed". That is fine for the tool and sandbox tests that only need somewhere
to read and write. It is *not* a repository, and now that ``commit`` lives on the
``Workspace`` seam, a provider that cannot commit has to refuse rather than hand
back a sha it made up.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from forgepilot.seams.refs import CommitAuthor, RepoRef, SessionId

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")


async def _load(config: dict) -> object:
    """The plugin, through the kernel, the way a profile loads it."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root

    root = create_root()
    await root.plugin(
        resolve_plugin("forgepilot.providers.workspace.local:LocalTempWorkspacePlugin"),
        config,
    )
    return root.workspace


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


async def test_a_workspace_is_not_created_where_the_caller_happened_to_stand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tempfile``'s last resort is refused rather than turned into a checkout.

    On a host whose sandbox denies every real temp directory, ``gettempdir()`` is the
    working directory — measured on this development box, where the repository root was
    the only creatable candidate and 14 ``forgepilot-*`` checkouts had accumulated in
    it. A provider that accepts that fallback creates a workspace inside whatever tree
    the caller stood in; the failure has to be a named refusal, because the alternative
    is silent and looks like a successful run.
    """
    monkeypatch.delenv("FORGEPILOT_TMP", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(Path.cwd()))

    with pytest.raises(RuntimeError, match="FORGEPILOT_TMP"):
        await _load({})


async def test_the_temporary_root_can_be_named_by_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refusal is a question with an answer, not a dead end."""
    monkeypatch.setattr(tempfile, "tempdir", str(Path.cwd()))
    monkeypatch.setenv("FORGEPILOT_TMP", str(tmp_path))

    workspace = await _load({})
    handle = await workspace.prepare(REPO, "main", SessionId("s-env"))

    assert handle.root.parent == tmp_path.resolve()


async def test_the_configured_base_dir_outranks_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A profile that names a directory gets that directory."""
    chosen, other = tmp_path / "chosen", tmp_path / "other"
    monkeypatch.setenv("FORGEPILOT_TMP", str(other))

    workspace = await _load({"base_dir": str(chosen)})
    handle = await workspace.prepare(REPO, "main", SessionId("s-base"))

    assert handle.root.parent == chosen.resolve()
