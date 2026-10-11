"""A session id is an identity, so two sessions must not share one.

The stamp alone was not unique, and the file the id names *is* the session, so two runs
starting inside the same second wrote into one file: the store appends, so nothing was
truncated and nothing was lost — the two event streams simply interleaved, and reading by
id returned a mixture of two sessions.

That is invisible on a machine where a session takes seconds to start, and immediate in a
batch of fast CLI runs, which is where it was found: eighteen `awaiting_info` rows inside a
4.5-second window left four session files, and three sequential eval runs of 0.2s left two.
"""

from __future__ import annotations

from datetime import UTC, datetime

from forgepilot.runtime import assembly


class _FrozenDatetime(datetime):
    """A clock that does not move, so "same second" is a fact rather than a hope."""

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206 - mirrors `datetime.now`
        return datetime(2026, 10, 11, 6, 47, 50, tzinfo=UTC)


def test_two_sessions_in_the_same_second_get_different_ids(monkeypatch) -> None:
    monkeypatch.setattr(assembly, "datetime", _FrozenDatetime)

    first = assembly.new_session_id()
    second = assembly.new_session_id()

    assert first != second, "two sessions in one second must not share an identity"
    # The stamp is still there: an id should stay sortable and readable in a log listing.
    assert str(first).startswith("session-20261011T064750-")


def test_the_id_is_not_only_random(monkeypatch) -> None:
    """The failure this guards against is dropping the stamp for a bare uuid.

    A random id with no timestamp would be unique and useless: session files are listed
    and read by name, and `202610/<session-id>.jsonl` is how a person finds last week's run.
    """
    monkeypatch.setattr(assembly, "datetime", _FrozenDatetime)

    assert "20261011T064750" in str(assembly.new_session_id())
