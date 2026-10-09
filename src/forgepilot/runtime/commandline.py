"""Turning a command line into an ``argv``, and deciding whether it gets a shell.

Two callers, and they want **opposite** things from the same answer:

- ``run_checks`` runs the ``project.yaml`` commands. These are lines a human
  wrote, and they are executed as an argv list with **no shell** — so a ``&&`` in
  one is a mistake rather than a feature, and the config says so.
- The ``run`` tool runs a *model's* line. Models write shell lines —
  ``pytest -q 2>&1 | tail -30`` is what one of them wrote — and handing that to
  ``exec`` without a shell passes ``2>&1`` to pytest as an argument.

Both used to split their own way, and the two disagreed. See ``split_command``
and ``shell_argv``.

It lives here rather than in either caller because it belongs to neither. It is
a fact about the platform.
"""

from __future__ import annotations

import os
import shlex

__all__ = ["shell_argv", "split_command"]


def split_command(command: str) -> list[str]:
    """Split a command line the way *this platform* defines one.

    ``shlex.split(posix=True)`` treats a backslash as an escape, so on Windows an
    absolute path loses its separators — ``C:\\…\\python.exe`` becomes
    ``C:…python.exe`` — the sandbox reports "command not found", and the check
    reads as a configuration problem rather than as the harness mangling its own
    argv. Production is Linux, where either form works; the dev machine is not,
    and a check that cannot run where you develop is a loop that never closes
    there.

    Non-posix mode preserves the path but keeps one layer of quoting, so that is
    removed. The result is the same argv on both platforms for the same command
    line, which is what "the same code" should mean.
    """
    if os.name == "nt":
        parts = shlex.split(command, posix=False)
        return [
            part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part
            for part in parts
        ]
    return shlex.split(command, posix=True)


def shell_argv(command: str) -> tuple[str, ...]:
    """The argv that runs ``command`` through *this platform's* shell.

    The `run` tool uses this; `run_checks` does not, and the asymmetry is the
    point. A ``project.yaml`` check is a line a human wrote and can read, and
    keeping it argv-only keeps it literal — which is what made ``&&`` in it a
    mistake rather than a feature. A model's ``command`` is a *shell line* by
    intent: it writes ``pytest -q 2>&1 | tail -30``, and a tool that hands that
    to ``exec`` without a shell passes ``2>&1`` to pytest as an argument. A real
    run did exactly that twice.

    **This adds expressiveness, not authority.** A model can already ask for any
    argv it likes; ``|`` buys it a pipeline it could have written by hand. The
    boundary that matters is the sandbox around the process, and that is
    unchanged — which is also why nothing is relaxed here.

    On Windows this is the platform's own ``cmd.exe`` (named by ``COMSPEC``)
    rather than Git's ``sh``. Git's shell is present on a developer box and not
    on a bare host, and — more importantly — it is MSYS, so it rewrites arguments
    that look like POSIX paths, which is the same behaviour that made ``wsl.exe``
    unusable as a transport. Production is Linux, where `/bin/sh` is the answer
    and neither caveat applies.
    """
    if os.name == "nt":
        return (os.environ.get("COMSPEC", "cmd.exe"), "/c", command)
    return ("/bin/sh", "-c", command)

