"""Engine-level tests — one per thing a user could complain about.

⚠️ **These name values, never places.** `policy/rules/` holds eight modules and the
failure this task turns on has no frame inside it: `check()` returns sentences, and
the sentence that should not be there does not say which module produced it. Finding
that module is the task, and it is the only task in the set where the module count
makes finding it cost anything.
"""

from __future__ import annotations

from policy.engine import broken_rules, check


def test_a_credential_that_breaks_nothing_has_nothing_to_report() -> None:
    assert check("7Qv!m2Rt#Kd9") == []


def test_a_credential_with_no_repeats_is_accepted() -> None:
    """Six characters, each used exactly twice and none of them three times. Every
    other rule here passes it, so the one message it comes back with is the one
    thing wrong with it — and that message is not true."""
    assert check("a1b2c3a1b2c3") == []


def test_a_short_credential_is_refused() -> None:
    assert "must be between 12 and 128 characters" in check("aB3!x")


def test_a_credential_without_a_digit_is_refused() -> None:
    assert "must contain at least one digit" in check("Qvw!mRt#KdPb")


def test_a_credential_with_too_few_distinct_characters_is_refused() -> None:
    assert "must contain at least 6 different characters" in check("aaaBBB111!!!")


def test_three_identical_characters_in_a_row_are_refused() -> None:
    """The rule this suite is about, from the side that is not broken: a genuine
    run of three is still caught."""
    assert "runs" in broken_rules("Qvvvm2Rt#Kd9p")


def test_the_audit_view_names_the_rule_the_message_came_from() -> None:
    assert broken_rules("7Qv!m2Rt#Kd9") == []
