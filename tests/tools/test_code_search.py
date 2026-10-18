"""The `code_search` tool: the retriever, finally reachable by the model.

Until this existed, the retriever seam had a real provider on one side and **no
consumer on the other** — the model could only read files whose paths it already
knew, which is not a code agent.

The shape follows Claude Code's Grep tool, because that shape was arrived at
against real use:

- **`output_mode` defaults to listing files, not matching lines.** A model handed
  forty matching lines when what it wanted to know was *which* files matter has
  spent its context on something it did not ask for.
- **A cap carries an explicit `truncated` marker.** Silently returning the first N
  reads as "that is all there is", and the model stops looking.

Both are reported back so the model can narrow rather than guess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgepilot.seams.refs import PrincipalId, RepoRef, TenantId, TurnId
from forgepilot.seams.security import (
    ExecutionConstraints,
    NetworkAccess,
    Operation,
    Principal,
)
from forgepilot.seams.tools import ToolCall

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")

FILES = {
    "src/login.py": (
        "MAX_ATTEMPTS = 3\n\n\n"
        "def retry_login(client, credentials):\n"
        "    for _ in range(MAX_ATTEMPTS):\n"
        "        return client.login(credentials)\n"
    ),
    "src/billing.py": "def charge(client):\n    return client.login()\n",
    "tests/test_login.py": "def test_retry():\n    assert retry_login\n",
}


@pytest.fixture
def checkout(rig) -> Path:
    """The files `code_search` will search — written into the rig's workspace."""
    for relative, content in FILES.items():
        target = rig.workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return rig.workspace


def constraints() -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset({REPO}),
        permitted_operations=frozenset({Operation.READ_CODE}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
    )


async def search(rig, **arguments):
    """Run the tool the way the loop does — through the pipeline."""
    return await rig.pipeline.invoke(
        ToolCall(call_id="c1", name="code_search", arguments_json=json.dumps(arguments)),
        session=rig.session,
        turn=TurnId(1),
        step=1,
        constraints=constraints(),
    )


# ---------------------------------------------------------------------------
# It exists
# ---------------------------------------------------------------------------
async def test_the_tool_is_offered_to_the_model(rig) -> None:
    names = {schema.name for schema in rig.registry.schemas()}

    assert "code_search" in names, (
        "with no tool exposing it the retriever is unreachable, and a model can only "
        "read files whose paths it already knows"
    )


async def test_it_finds_a_pattern_across_the_checkout(rig, checkout) -> None:
    outcome = await search(rig, pattern="retry_login", output_mode="content")

    assert not outcome.is_error, outcome.content
    assert "src/login.py" in outcome.content
    assert "def retry_login" in outcome.content


# ---------------------------------------------------------------------------
# output_mode — the default keeps context small on purpose
# ---------------------------------------------------------------------------
async def test_the_default_lists_files_rather_than_lines(rig, checkout) -> None:
    outcome = await search(rig, pattern="login")

    assert "src/login.py" in outcome.content
    assert "tests/test_login.py" in outcome.content
    assert "def retry_login" not in outcome.content, (
        "the default mode reproduced match text the caller did not ask for"
    )


async def test_content_mode_shows_the_matching_lines_with_their_numbers(rig, checkout) -> None:
    outcome = await search(rig, pattern="MAX_ATTEMPTS", output_mode="content")

    assert "src/login.py:1" in outcome.content
    assert "MAX_ATTEMPTS = 3" in outcome.content


async def test_count_mode_says_how_many_per_file(rig, checkout) -> None:
    outcome = await search(rig, pattern="login", output_mode="count")

    assert "src/login.py" in outcome.content
    assert "2" in outcome.content, "login appears twice in that file"


async def test_an_unknown_output_mode_is_refused_and_the_valid_ones_named(rig, checkout) -> None:
    outcome = await search(rig, pattern="login", output_mode="loud")

    assert outcome.is_error
    assert "files_with_matches" in outcome.content


# ---------------------------------------------------------------------------
# Caps, and honesty about them
# ---------------------------------------------------------------------------
async def test_a_result_cap_is_reported_rather_than_silently_applied(rig, checkout) -> None:
    outcome = await search(rig, pattern="login", output_mode="content", max_results=1)

    assert outcome.meta.get("truncated") is True
    assert "truncated" in outcome.content.lower(), (
        "a silent cap reads as 'that is all there is'"
    )


async def test_no_matches_is_an_empty_answer_not_an_error(rig, checkout) -> None:
    outcome = await search(rig, pattern="this_pattern_is_nowhere")

    assert not outcome.is_error
    assert "no match" in outcome.content.lower()
    assert outcome.meta.get("hits") == 0


# ---------------------------------------------------------------------------
# Glob narrowing
# ---------------------------------------------------------------------------
async def test_a_glob_narrows_which_files_are_searched(rig, checkout) -> None:
    outcome = await search(rig, pattern="login", glob="tests/**")

    assert "tests/test_login.py" in outcome.content
    assert "src/login.py" not in outcome.content


# ---------------------------------------------------------------------------
# Permission, checked here as well as in the seam
# ---------------------------------------------------------------------------
async def test_a_repository_outside_the_grant_is_refused(rig, checkout) -> None:
    """The seam re-checks; so does the tool. A tool bug must not read what the
    principal may not."""
    outcome = await search(rig, pattern="x", repo="git.corp/team/elsewhere")

    assert outcome.is_error
    assert "may search" in outcome.content
    # And it must not confirm whether that repository exists: "no checkout is
    # configured" would answer a question the caller is not entitled to ask.
    assert "checkout" not in outcome.content
    assert "configured" not in outcome.content


async def test_it_searches_the_working_tree_not_the_configured_checkout(
    rig, checkout, tmp_path: Path
) -> None:
    """The rig keeps the two apart on purpose, so this can be asserted at all.

    `checkout` is the *working tree* — where the session edits. The retriever's
    configured root is a different directory, standing in for where the
    repository lives. Both contain `src/login.py`, with different content; only
    the working tree's may be found.

    This is the shape of a real failure. `code_search` searched the configured
    path while the agent edited a worktree, and on a repo host whose mirror was
    bare that meant searching nothing at all — reported as "no match", which
    reads as "that symbol does not exist". The model then spent eleven shell
    calls improvising, and wandered out of the workspace.
    """
    elsewhere = tmp_path / "checkout" / "src"
    elsewhere.mkdir(parents=True, exist_ok=True)
    (elsewhere / "login.py").write_text(
        "def retry_login():\n    return 'THE OTHER COPY'\n", encoding="utf-8"
    )

    stale = await search(rig, pattern="THE OTHER COPY", output_mode="content")
    # Asserted on `hits`, not on the absence of the string: a "no match" message
    # *echoes the pattern*, so `"THE OTHER COPY" not in content` is false whether
    # or not anything was found. The first version of this test did exactly that
    # and failed against its own error text.
    assert stale.meta["hits"] == 0, "the configured checkout was searched, not the working tree"

    live = await search(rig, pattern="MAX_ATTEMPTS", output_mode="content")
    assert "src/login.py" in live.content, "the working tree was not searched"


async def test_every_answer_says_which_revision_was_searched(rig, checkout) -> None:
    """A hit the model cannot address is a dead end.

    `code_search` used to render `path:line: text` and drop both the repository and
    the ref, so the one field `read_code` needs to read what was found never reached
    the caller — and `seams/retriever.py` is explicit that every hit carries its ref.
    """
    outcome = await search(rig, pattern="login")

    assert not outcome.is_error, outcome.content
    assert f"[served from: {REPO.slug}@" in outcome.content


async def test_a_working_tree_that_is_not_there_is_refused(rig, checkout) -> None:
    """Refused, not silently ignored. `read_file` is bounded to the workspace,
    and search has to be too — it is a read primitive by another name.

    The pipeline turns the provider's refusal into an error *result* rather than
    an exception, which is what the model sees; asserting on the outcome is
    asserting on the thing that actually happens.
    """
    rig.pipeline.workspace_root = Path("C:/definitely-not-here")

    outcome = await search(rig, pattern="retry_login")

    assert outcome.is_error
    assert "UnknownCheckout" in outcome.content
    assert "not a directory" in outcome.content
