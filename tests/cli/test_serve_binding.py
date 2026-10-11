"""The API's binding is a security decision, so it is not made silently.

`forgepilot --serve --host 0.0.0.0` used to say nothing about what it had just
exposed: a bearer token over plain HTTP, and a session log full of proprietary
source. It still does not *refuse* — fronting this with a TLS proxy on a trusted
network is a legitimate deployment, and refusing would be this code inventing a
policy nobody decided — but it now says what changed. Same shape as
`--allow-uncontained-exec`, which also warns rather than blocking.

The warning is tested through its own function rather than by starting a server,
because booting uvicorn to read a line of stderr would mostly test uvicorn.
"""

from __future__ import annotations

import pytest

from forgepilot.cli.main import _warn_if_the_binding_is_not_loopback


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "localhost"])
def test_a_loopback_binding_is_silent(host: str, capsys) -> None:
    """Nothing is said about the default, which is what makes it usable as one.

    A warning that fired on `127.0.0.1` would train the reader to ignore it.
    """
    _warn_if_the_binding_is_not_loopback(host, 8000)

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.7", "api.internal", ""])
def test_a_reachable_binding_says_what_it_exposes(host: str, capsys) -> None:
    """The message has to name the things that make this a decision rather than
    generic hygiene advice: the credential, the missing TLS, and the log."""
    _warn_if_the_binding_is_not_loopback(host, 8000)

    captured = capsys.readouterr()
    assert captured.out == "", (
        "a warning on stdout is a warning in a pipe nobody reads"
    )
    assert "not loopback" in captured.err
    assert "bearer token" in captured.err
    assert "TLS" in captured.err
    assert "session log" in captured.err
    assert "127.0.0.1" in captured.err, "a warning with no remedy is a complaint"


def test_the_warning_names_the_address_it_is_about(capsys) -> None:
    """An operator running several servers has to know which one this is."""
    _warn_if_the_binding_is_not_loopback("0.0.0.0", 9001)

    captured = capsys.readouterr()
    assert "0.0.0.0:9001" in captured.err
