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
