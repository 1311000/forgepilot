"""Turning a command line into an ``argv``.

Two callers need this and they must agree: the repair loop's ``project.yaml``
checks, and the ``run`` tool's ``command`` argument. If they split differently,
the same command line means one thing to the harness and another to the model —
and the failure looks like whichever one is being blamed.

It lives here rather than in either of them because it belongs to neither. It is
a fact about the platform.
"""

from __future__ import annotations

import os
import shlex

__all__ = ["SHELL_OPERATORS", "looks_like_shell", "shell_hint", "split_command"]


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


#: Characters that mean "let a shell interpret this line". Nothing in this
#: codebase ever gives a command line a shell — see the sandbox notes — so an
#: operator here is not a syntax error, it is a *literal argument*, and the
#: command that results is almost never the one that was meant.
SHELL_OPERATORS: tuple[tuple[str, str], ...] = (
    ("&&", "run these in sequence"),
    ("||", "run the second if the first fails"),
    ("|", "pipe the output into the next command"),
    (">>", "append the output to a file"),
    (">", "redirect the output to a file"),
    ("<", "feed a file in as input"),
    (";", "separate two commands"),
    ("`", "substitute a command's output"),
    ("$(", "substitute a command's output"),
)


def looks_like_shell(command: str) -> tuple[str, str] | None:
    """The first shell operator in ``command``, and what it was probably for.

    Reported rather than rejected. There is no shell to reject it *for*, and the
    model has already been given the tool — what it lacks is the information that
    the operator will arrive as an ordinary argument, which is why a request for
    ``ls -la && find .`` came back as ``ls: unknown option -- e``.
    """
    for operator, meaning in SHELL_OPERATORS:
        if operator in command:
            return operator, meaning
    return None


def shell_hint(command: str) -> str:
    """A sentence to append when the command used shell syntax, or ``''``.

    Appended to the result whether or not the command ran, because both happen:
    ``ls -la && find .`` *did* execute — as ``ls`` with five odd arguments — and
    answered with ``ls: unknown option -- e``, which says nothing about the `&&`
    that caused it.
    """
    found = looks_like_shell(command)
    if found is None:
        return ""
    operator, meaning = found
    return (
        f"\n\n[this command ran as an argv list, not through a shell: {operator!r} was "
        f"passed to the program as an ordinary argument instead of '{meaning}'. Nothing "
        f"here interprets shell syntax. Run the commands as separate calls.]"
    )
