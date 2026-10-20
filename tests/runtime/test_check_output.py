"""A failed check, reduced to what a repair loop can act on (P7).

Two mechanisms, tested together because they fail together in the sweep: the
output is **bounded** before it reaches the log, and the failing tests are
**named** before they reach the model.

The bug this is written against is a shape, not a crash: `run_checks` copied
whatever the runner printed into `CheckResult.output`, which is written to the
session log and replayed into every later request. A task whose checks fail four
times pays for four full tracebacks, and the one line that matters — which test
failed — is buried in the middle of each.
"""

from __future__ import annotations

from forgepilot.runtime.checkoutput import MAX_CHECK_OUTPUT_CHARS, bound, failed_tests

PYTEST_FAILURE = """\
============================= test session starts ==============================
collected 3 items

tests/test_paging.py::test_clamp FAILED                                  [ 33%]
tests/test_paging.py::test_page_size PASSED                              [ 66%]
tests/test_limits.py::test_band PASSED                                   [100%]

=================================== FAILURES ===================================
_____________________________ test_clamp ______________________________

    def test_clamp():
>       assert clamp(5_000) == 250
E       assert 500 == 250

=========================== short test summary info ============================
FAILED tests/test_paging.py::test_clamp - AssertionError: assert 500 == 250
========================= 1 failed, 2 passed in 0.12s =========================
"""

UNITTEST_FAILURE = """\
F.
======================================================================
FAIL: test_clamp (tests.test_paging.TestClamp.test_clamp)
----------------------------------------------------------------------
Traceback (most recent call last):
AssertionError: 500 != 250

----------------------------------------------------------------------
Ran 2 tests in 0.001s

FAILED (failures=1)
"""


# ---------------------------------------------------------------------------
# Naming what failed
# ---------------------------------------------------------------------------
def test_a_pytest_failure_is_named_by_its_id() -> None:
    assert failed_tests(PYTEST_FAILURE) == ("tests/test_paging.py::test_clamp",)


def test_a_unittest_failure_is_named_with_its_class() -> None:
    assert failed_tests(UNITTEST_FAILURE) == ("tests.test_paging.TestClamp.test_clamp",)


def test_a_pass_names_nothing() -> None:
    assert failed_tests("3 passed in 0.10s") == ()


def test_an_unrecognised_runner_names_nothing() -> None:
    """A guessed test name sends the agent to a file that does not exist."""
    assert failed_tests("error: cannot find module 'left-pad'") == ()


def test_ids_are_deduplicated_and_keep_their_order() -> None:
    output = (
        "FAILED tests/a.py::test_one - boom\n"
        "FAILED tests/b.py::test_two - boom\n"
        "FAILED tests/a.py::test_one - boom\n"
    )
    assert failed_tests(output) == ("tests/a.py::test_one", "tests/b.py::test_two")


def test_a_collection_error_is_named_too() -> None:
    assert failed_tests("ERROR tests/test_broken.py::test_x - ImportError") == (
        "tests/test_broken.py::test_x",
    )


def test_the_list_is_bounded() -> None:
    output = "".join(f"FAILED tests/a.py::test_{i} - boom\n" for i in range(200))
    assert len(failed_tests(output)) == 50


# ---------------------------------------------------------------------------
# Bounding the output
# ---------------------------------------------------------------------------
def test_short_output_is_untouched() -> None:
    assert bound("1 failed", limit=100) == "1 failed"


def test_the_head_and_the_tail_both_survive() -> None:
    output = "HEAD" + ("x" * 5_000) + "TAIL"
    bounded = bound(output, limit=200)
    assert bounded.startswith("HEAD"), "the summary at the top was dropped"
    assert bounded.endswith("TAIL"), "the last traceback was dropped"
    assert len(bounded) <= 200 + 200, "the marker may exceed the limit, not the body"
    assert "characters omitted" in bounded


def test_the_marker_says_how_much_was_dropped() -> None:
    output = "a" * 300
    bounded = bound(output, limit=100)
    assert "200 characters omitted" in bounded


def test_the_default_limit_is_the_documented_one() -> None:
    assert MAX_CHECK_OUTPUT_CHARS == 20_000
    assert bound("a" * 25_000).count("characters omitted") == 1


# ---------------------------------------------------------------------------
# The two together, as `run_checks` uses them
# ---------------------------------------------------------------------------
async def test_a_check_result_names_the_failures_and_bounds_its_output(tmp_path) -> None:
    """The wiring, not the helpers: `run_checks` must do both."""
    from forgepilot.providers.sandbox.scripted import ScriptedResult
    from forgepilot.runtime.repair import run_checks
    from forgepilot.seams.refs import SessionId, TenantId
    from forgepilot.seams.sandbox import SandboxEnforcement
    from forgepilot.seams.security import ExecutionConstraints, NetworkAccess, Operation, Principal

    class Outcome(ScriptedResult):
        """`repair.run_checks` reads `enforcement` and `executed` off the result."""

        executed = True
        timed_out = False
        enforcement = SandboxEnforcement.FULL

    class Sandbox:
        def __init__(self) -> None:
            self.results = [Outcome(exit_code=1, stderr=PYTEST_FAILURE)]

        async def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            return self.results.pop(0)

    class Session:
        def __init__(self) -> None:
            self.events: list[tuple] = []

        async def append(self, kind, data, **kwargs):  # noqa: ANN001, ANN003
            self.events.append((kind, data))

    principal = Principal(
        principal_id="alice",  # type: ignore[arg-type]
        tenant_id=TenantId("acme"),
    )
    constraints = ExecutionConstraints.for_principal(
        principal,
        permitted_operations=frozenset({Operation.RUN_TESTS}),
        network=NetworkAccess.DENY,
    )
    session = Session()

    result = await run_checks(
        commands=["python -m pytest -q"],
        sandbox=Sandbox(),
        session=session,
        constraints=constraints,
        workspace_root=tmp_path,
        session_id=SessionId("s-1"),
    )

    assert not result.passed
    assert result.meta["failedTests"] == ["tests/test_paging.py::test_clamp"]
    assert "failing tests: tests/test_paging.py::test_clamp" in result.output
    assert result.output.index("failing tests:") < result.output.index("assert 500")
