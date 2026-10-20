"""The file-backed ticket source: tickets a deployment can actually open.

The mock's fixtures are code — a test registers them by calling ``add_ticket``.
Without something like this provider, ``forgepilot --task BUG-1`` has no ticket to
fetch on any profile that is not a test, and the one ticket the example runs
would have to live in a Python literal.

What these tests pin is mostly the *failure* behaviour, because that is where a
ticket source does damage: an empty directory that returns nothing looks exactly
like a tracker that does not have the ticket, and a source that silently accepts
a malformed file moves the failure to an hour into a batch run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgepilot.seams.refs import TicketId

TICKET = """\
id: BUG-1
title: moving_average 少返回一个窗口
priority: high
description: |
  复现步骤：
  1. 调用 moving_average([1, 2, 3, 4], 2)
  预期结果：3 个窗口
  实际结果：只有 2 个
  受影响仓库：git.local/demo/stats
"""


async def make_source(tmp_path: Path, files: dict[str, str], **config):
    """Boot the provider the way a profile would, and hand back the service."""
    from forgepilot.config import resolve_plugin
    from forgepilot.kernel import create_root

    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    root = create_root()
    fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.tickets.file:FileTicketSourcePlugin"),
        {"root": str(tmp_path), **config},
    )
    return root, fiber


# ---------------------------------------------------------------------------
# Reading a ticket
# ---------------------------------------------------------------------------
async def test_a_ticket_file_becomes_the_seams_own_ticket(tmp_path: Path) -> None:
    root, fiber = await make_source(tmp_path, {"BUG-1.yaml": TICKET})
    try:
        ticket = await root.tickets.fetch(TicketId("BUG-1"))

        assert ticket.id == TicketId("BUG-1")
        assert ticket.title == "moving_average 少返回一个窗口"
        assert "复现步骤" in ticket.description_md
        assert ticket.priority.value == "high"
        # The verbatim payload is kept, because the doc treats training a local
        # classifier on real history as a goal — and that needs the source text.
        assert ticket.raw["title"] == ticket.title
    finally:
        from forgepilot.kernel import dispose_fiber

        await dispose_fiber(fiber)


async def test_the_file_name_is_the_id_when_the_file_does_not_say(tmp_path: Path) -> None:
    root, fiber = await make_source(tmp_path, {"T-42.yaml": "title: something\n"})
    try:
        assert (await root.tickets.fetch(TicketId("T-42"))).title == "something"
    finally:
        from forgepilot.kernel import dispose_fiber

        await dispose_fiber(fiber)


async def test_a_ticket_that_passes_preprocessing_is_run_without_a_human(
    tmp_path: Path,
) -> None:
    """The example's ticket, checked against the rules that gate task mode.

    A ticket missing reproduction steps or an expected result is refused by
    deterministic preprocessing and never reaches the classifier — so a fixture
    ticket that has drifted would make the example stop at AWAITING_INFO while
    looking like the model refused. This asserts the *rules*, which is the part a
    text edit can break silently.
    """
    from forgepilot.orchestration.preprocess import preprocess
    from forgepilot.seams.refs import RepoRef

    repo = RepoRef(host="git.local", namespace="demo", name="stats")
    root, fiber = await make_source(tmp_path, {"BUG-1.yaml": TICKET})
    try:
        ticket = await root.tickets.fetch(TicketId("BUG-1"))
        result = preprocess(ticket, known_repos=(repo,))

        assert not result.needs_human, result.reason
        assert result.repos == (repo,), "the ticket must name the repository it is about"
    finally:
        from forgepilot.kernel import dispose_fiber

        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# Read-only, and loud about it
# ---------------------------------------------------------------------------
async def test_it_declares_only_the_capability_it_has(tmp_path: Path) -> None:
    root, fiber = await make_source(tmp_path, {"BUG-1.yaml": TICKET})
    try:
        assert root.tickets.capabilities() == frozenset({"read"})
    finally:
        from forgepilot.kernel import dispose_fiber

        await dispose_fiber(fiber)


@pytest.mark.parametrize("method", ["comment", "transition"])
async def test_writing_back_is_refused_rather_than_quietly_done(
    tmp_path: Path, method: str
) -> None:
    """A comment that vanishes is worse than one refused.

    There is no tracker behind this provider, so returning a fabricated
    ``CommentRef`` would put a comment id in the session log as evidence of
    something that never happened.
    """
    from forgepilot.providers.tickets.file import ReadOnlyTicketSource
    from forgepilot.seams.tickets import TicketStatus

    root, fiber = await make_source(tmp_path, {"BUG-1.yaml": TICKET})
    try:
        with pytest.raises(ReadOnlyTicketSource) as caught:
            if method == "comment":
                await root.tickets.comment(TicketId("BUG-1"), "hello")
            else:
                await root.tickets.transition(TicketId("BUG-1"), TicketStatus.CLOSED)

        assert "cannot write back" in str(caught.value)
    finally:
        from forgepilot.kernel import dispose_fiber

        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# Failing in a way that names the problem
# ---------------------------------------------------------------------------
async def test_an_unknown_ticket_lists_the_ones_that_exist(tmp_path: Path) -> None:
    """`no such ticket` with forty files on disk leaves the reader to guess."""
    root, fiber = await make_source(
        tmp_path, {"BUG-1.yaml": TICKET, "BUG-2.yaml": "title: other\n"}
    )
    try:
        with pytest.raises(KeyError) as caught:
            await root.tickets.fetch(TicketId("BUG-9"))

        assert "BUG-1" in str(caught.value) and "BUG-2" in str(caught.value)
    finally:
        from forgepilot.kernel import dispose_fiber

        await dispose_fiber(fiber)


async def test_an_empty_directory_is_an_error_not_an_empty_source(tmp_path: Path) -> None:
    """The failure that reads as a tracker problem rather than a config one."""
    from forgepilot.config.errors import ConfigError

    with pytest.raises(ConfigError) as caught:
        await make_source(tmp_path, {})

    assert "no ticket files" in str(caught.value)


async def test_a_malformed_file_names_the_file_that_was_malformed(tmp_path: Path) -> None:
    """Bisecting forty files by hand is the cost of not naming this."""
    from forgepilot.config.errors import ConfigError

    with pytest.raises(ConfigError) as caught:
        await make_source(tmp_path, {"T-1.yaml": "title: [unclosed\n"})

    assert "T-1.yaml" in str(caught.value)


async def test_two_files_claiming_one_id_is_refused(tmp_path: Path) -> None:
    """An override would be silent; a collision is a mistake worth stopping on."""
    from forgepilot.config.errors import ConfigError

    with pytest.raises(ConfigError) as caught:
        # Two files, each naming `BUG-1` in its own contents.
        await make_source(tmp_path, {"a.yaml": TICKET, "b.yaml": TICKET})

    assert "already defined" in str(caught.value)


async def test_two_names_for_one_field_is_refused(tmp_path: Path) -> None:
    """`description` and `description_md` are the same field; picking one by dict
    order is not a decision anyone made."""
    from forgepilot.config.errors import ConfigError

    with pytest.raises(ConfigError) as caught:
        await make_source(
            tmp_path,
            {"T-1.yaml": "title: t\ndescription: a\ndescription_md: b\n"},
        )

    assert "same field under two names" in str(caught.value)


async def test_an_unknown_status_names_the_allowed_ones(tmp_path: Path) -> None:
    from forgepilot.config.errors import ConfigError

    with pytest.raises(ConfigError) as caught:
        await make_source(tmp_path, {"T-1.yaml": "title: t\nstatus: in-progress\n"})

    assert "new" in str(caught.value) and "closed" in str(caught.value)
