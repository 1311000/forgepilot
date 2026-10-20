"""Retrying a failing check, and never letting the retry look clean.

The whole point of ``check_attempts`` is that a project's test command is sometimes
flaky — and the whole danger of it is that a second run can turn a failure into a
green check nobody remembers was red. These tests hold both halves: the retry
happens, and the result says it happened.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgepilot.runtime.repair import CheckResult, run_checks_until_stable


def _results(*results: CheckResult) -> tuple[list[CheckResult], list[int]]:
    """A ``run`` callable yielding ``results`` in order, and a call counter."""
    remaining = list(results)
    calls: list[int] = []

    async def run() -> CheckResult:
        calls.append(len(calls) + 1)
        return remaining.pop(0)

    return [run], calls  # type: ignore[list-item]


def _failed(exit_code: int = 1, output: str = "1 failed") -> CheckResult:
    return CheckResult(
        passed=False,
        commands=("pytest -q",),
        command="pytest -q",
        output=output,
        exit_code=exit_code,
        meta={"failedTests": ["tests/test_x.py::test_y"]},
    )


def _passed() -> CheckResult:
    return CheckResult(passed=True, commands=("pytest -q",), command="pytest -q", exit_code=0)


async def test_one_attempt_is_the_default_and_a_failure_stays_one() -> None:
    (run,), calls = _results(_failed())

    result = await run_checks_until_stable(run)

    assert calls == [1]
    assert result.passed is False
    assert result.meta["attempts"] == 1
    assert "flaky" not in result.meta


async def test_a_pass_on_the_second_attempt_is_flaky_not_clean() -> None:
    """The verdict is a pass, and the *record* says the first run disagreed."""
    (run,), calls = _results(_failed(exit_code=1), _passed())

    result = await run_checks_until_stable(run, attempts=2)

    assert calls == [1, 2]
    assert result.passed is True
    assert result.meta["flaky"] is True
    assert result.meta["attempts"] == 2
    assert result.meta["firstExitCode"] == 1
    assert result.meta["firstFailedTests"] == ["tests/test_x.py::test_y"]
    # Both runs are in the text the model and the pull request body quote.
    assert "flaky" in result.output
    assert "attempt 1 (failed, exit 1)" in result.output
    assert "attempt 2 (passed)" in result.output


async def test_a_failure_that_stays_failing_is_not_called_flaky() -> None:
    """Two failing runs agree; there is nothing unstable to report."""
    (run,), calls = _results(_failed(exit_code=2), _failed(exit_code=3))

    result = await run_checks_until_stable(run, attempts=2)

    assert calls == [1, 2]
    assert result.passed is False
    assert result.exit_code == 3
    assert result.meta["attempts"] == 2
    assert "flaky" not in result.meta


async def test_a_check_that_could_not_run_is_not_retried() -> None:
    """``could_not_run`` is a configuration fact. Asking twice learns nothing."""
    (run,), calls = _results(
        CheckResult(passed=False, could_not_run=True, reason="no permission to run tests")
    )

    result = await run_checks_until_stable(run, attempts=3)

    assert calls == [1]
    assert result.could_not_run is True


async def test_a_timeout_is_not_retried() -> None:
    """A timeout is a budget fact, and a retry spends the budget to learn nothing."""
    (run,), calls = _results(
        CheckResult(passed=False, output="timed out", meta={"timed_out": True})
    )

    result = await run_checks_until_stable(run, attempts=3)

    assert calls == [1]
    assert result.meta.get("timed_out") is True
    assert "flaky" not in result.meta


async def test_the_third_attempt_can_be_the_one_that_passes() -> None:
    (run,), calls = _results(_failed(), _failed(), _passed())

    result = await run_checks_until_stable(run, attempts=3)

    assert calls == [1, 2, 3]
    assert result.passed is True
    assert result.meta["attempts"] == 3
    assert result.meta["flaky"] is True


async def test_a_retry_that_stops_being_runnable_reports_that() -> None:
    """Attempt 2 discovering the sandbox cannot execute is an answer, not noise."""
    (run,), calls = _results(_failed(), CheckResult(passed=False, could_not_run=True))

    result = await run_checks_until_stable(run, attempts=3)

    assert calls == [1, 2]
    assert result.could_not_run is True
    assert "flaky" not in result.meta


async def test_zero_attempts_is_refused() -> None:
    (run,), _ = _results(_passed())

    with pytest.raises(ValueError, match="at least 1"):
        await run_checks_until_stable(run, attempts=0)


# ---------------------------------------------------------------------------
# The pull request body says it
# ---------------------------------------------------------------------------
def test_the_pull_request_body_says_the_check_was_flaky() -> None:
    """A reviewer deciding whether to trust a green check has to know this."""
    from forgepilot.orchestration.modes.task import _pull_request_body

    plan = SimpleNamespace(
        ticket=SimpleNamespace(id="BUG-1"),
        repos=(),
        understanding=None,
    )
    flaky = CheckResult(
        passed=True,
        commands=("pytest -q",),
        command="pytest -q",
        exit_code=0,
        meta={"flaky": True, "attempts": 2},
    )

    body = _pull_request_body(plan, (flaky,), ("src/only.py",), "")

    assert "- `pytest -q` — passed on attempt 2" in body
    assert "flaky" in body
