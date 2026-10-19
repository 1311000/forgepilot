"""The sweep refuses to put its own answers a relative path away.

The M23 matrix measured three cells whose agent had read
`evals/tasks/not-in-the-checkout/solution/` through the shell — a pass in every column, and
indistinguishable from a real one. The cause was not the harness's default (`scratch_root()`
has pointed outside the checkout for several milestones) but the setting this repository's
notes recommend for `--boot`: `FORGEPILOT_HOME=.tmp/fp-home` is inside the checkout, and the
scratch follows it.

These tests pin the refusal, and the escape hatch that is deliberately awkward.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FORGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FORGE / "evals"))

import harness as H  # noqa: E402


def test_the_default_scratch_is_outside_the_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGEPILOT_EVAL_SCRATCH", raising=False)
    monkeypatch.setenv("FORGEPILOT_HOME", str(Path.home() / ".forgepilot-test-home"))

    assert H.scratch_is_inside_the_repository() is False


def test_a_scratch_under_the_checkout_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGEPILOT_EVAL_SCRATCH", str(H.FORGE / ".tmp" / "eval-scratch"))

    assert H.scratch_is_inside_the_repository() is True


def test_the_refusal_names_the_answer_and_two_ways_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(H.ALLOW_INSIDE_REPO, raising=False)
    monkeypatch.setenv("FORGEPILOT_EVAL_SCRATCH", str(H.FORGE / ".tmp" / "eval-scratch"))

    with pytest.raises(RuntimeError) as caught:
        H.refuse_scratch_inside_the_repository()

    message = str(caught.value)
    assert "evals/tasks/<name>/solution/" in message
    assert "FORGEPILOT_EVAL_SCRATCH" in message
    assert H.ALLOW_INSIDE_REPO in message


def test_the_escape_hatch_still_scores_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The switch exists, and it is the only thing that turns the refusal off — a warning
    would have been read as advice and the rows would have been quoted as measurements."""
    monkeypatch.setenv("FORGEPILOT_EVAL_SCRATCH", str(H.FORGE / ".tmp" / "eval-scratch"))
    monkeypatch.setenv(H.ALLOW_INSIDE_REPO, "1")

    H.refuse_scratch_inside_the_repository()


def test_a_path_outside_the_checkout_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tmp_path` is *not* outside: this box pins the temp root to `.tmp/tmp` inside the
    checkout (see `tests/contract/test_the_temp_root_is_not_the_repository.py`), which is
    exactly the trap the guard is about. A sibling of the checkout is the honest stand-in."""
    outside = H.FORGE.parent / "forgepilot-eval-test"
    monkeypatch.setenv("FORGEPILOT_EVAL_SCRATCH", str(outside))
    monkeypatch.delenv(H.ALLOW_INSIDE_REPO, raising=False)

    H.refuse_scratch_inside_the_repository()
