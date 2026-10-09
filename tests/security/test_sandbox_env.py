"""What a sandboxed command is allowed to see of our environment.

Both call sites — the `run` tool and the harness's own checks — passed
``env={}``. That was never a working value, it was an unexercised one: with no
``PATH`` a bare ``pytest`` cannot be resolved, and on Windows a child Python
without ``SYSTEMROOT`` fails to import ``_overlapped`` and dies before reaching
a single test. Nothing noticed while the only sandboxes answered from a script.

The obvious repair — hand over ``os.environ`` — is worse than the disease. The
model can ask for a command, and the parent environment holds
``FORGEPILOT_LLM_API_KEY``, ``FORGEPILOT_SIGNING_KEY`` and the dev token table.
A single ``env`` call would print them into a ``tool/result`` that is written to
the session log and shown to the model. So the environment is an **allow-list**:
a variable nobody thought about is not inherited, rather than being inherited
until somebody remembers to add it to a deny-list.
"""

from __future__ import annotations

from forgepilot.runtime.sandbox_env import command_env


def test_a_command_gets_the_variables_it_needs_to_start(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = command_env()

    assert env["PATH"] == "/usr/bin:/bin"


def test_a_command_never_inherits_the_harness_credentials(monkeypatch) -> None:
    """`env` is a valid thing for the model to run, and its output is logged."""
    monkeypatch.setenv("FORGEPILOT_LLM_API_KEY", "sk-live-secret")
    monkeypatch.setenv("FORGEPILOT_SIGNING_KEY", "signing-secret")
    monkeypatch.setenv("FORGEPILOT_DEV_TOKENS", '[{"token": "dev-token"}]')
    monkeypatch.setenv("PATH", "/usr/bin")

    env = command_env()

    assert not [k for k in env if k.startswith("FORGEPILOT_")]
    assert "sk-live-secret" not in " ".join(env.values())
    # ...while still being usable.
    assert env["PATH"] == "/usr/bin"


def test_an_unknown_variable_is_not_inherited(monkeypatch) -> None:
    """The allow-list is what decides, not a list of things we remembered to ban.

    A deny-list leaks every secret nobody thought of yet, which is the failure
    mode this shape exists to avoid.
    """
    monkeypatch.setenv("SOME_FUTURE_INTERNAL_TOKEN", "oops")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "oops")

    env = command_env()

    assert "SOME_FUTURE_INTERNAL_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_it_reads_the_process_environment_when_none_is_given(monkeypatch) -> None:
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    assert command_env()["LANG"] == "en_US.UTF-8"
    assert command_env({"LANG": "C"})["LANG"] == "C"


# ---------------------------------------------------------------------------
# "the whole workspace" has to be expressible
# ---------------------------------------------------------------------------
def test_a_dot_rule_means_the_whole_workspace() -> None:
    """⚠️ `<project>.yaml` says `writable_paths: ["."]` for "everything here".

    It silently meant *nothing*. `normalise_relative(".")` returns `None`, and
    `allows_write_to` skipped any rule that normalised to `None` — so the obvious
    spelling was discarded without a word, and **every write was refused**.

    Found by running a real task: the model tried eleven different spellings of a
    `stats.py` path, each answered `outside the caller's writable set`, and gave
    up honestly. Nothing in the message said what the set actually was.
    """
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import ExecutionConstraints, Principal

    constraints = ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("demo"), tenant_id=TenantId("local")),
        writable_paths=(".",),
    )

    assert constraints.allows_write_to("stats.py")
    assert constraints.allows_write_to("src/deep/nested.py")


def test_a_dot_rule_does_not_widen_to_an_absolute_path() -> None:
    """"Everything under the workspace" is not "everything"."""
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import ExecutionConstraints, Principal

    constraints = ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("demo"), tenant_id=TenantId("local")),
        writable_paths=(".",),
    )

    assert not constraints.allows_write_to("/etc/passwd")
    assert not constraints.allows_write_to("../../escape.py")


def test_an_empty_writable_set_is_still_read_only() -> None:
    """The existing rule, which the fix must not quietly undo: absent means
    nothing is writable, not everything."""
    from forgepilot.seams.refs import PrincipalId, TenantId
    from forgepilot.seams.security import ExecutionConstraints, Principal

    constraints = ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("demo"), tenant_id=TenantId("local")),
        writable_paths=(),
    )

    assert not constraints.allows_write_to("stats.py")
