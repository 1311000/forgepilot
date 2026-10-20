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

A third job lives here too — :func:`programs`, which reads a line as the set of
programs it would start, so that a declared command allow-list has something to
be checked against. It belongs with the other two because it is the same fact
about the platform, read in the other direction.

It lives here rather than in either caller because it belongs to neither. It is
a fact about the platform.
"""

from __future__ import annotations

import os
import re
import shlex

from forgepilot.seams.security import normalise_program

__all__ = ["UnparsableCommand", "programs", "shell_argv", "split_command"]


class UnparsableCommand(ValueError):
    """A shell line that cannot be attributed to a list of programs.

    Raised only by :func:`programs`, and only when a *command policy is declared*
    — the guard asks this question, and "I cannot tell" has to be distinguishable
    from "nothing to check" or a declared allow-list would be a suggestion.
    """


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
            part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part for part in parts
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


# ---------------------------------------------------------------------------
# Decomposing a shell line into the programs it would start.
#
# One reader: ``runtime/guards/builtin.py::command_scope_guard``, which has to
# answer "which programs does this line start?" against a declared allow-list.
#
# It is deliberately *not* a shell parser. It is a conservative approximation
# whose failures all point the same way — refuse — because the alternative to a
# conservative answer is a policy that can be walked around. What it cannot
# attribute, it will not bless: see ``UnparsableCommand``.
# ---------------------------------------------------------------------------

#: `VAR=value` in command position: an assignment, not a program.
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

#: `>`, `>>`, `<`, `<<`, `&>` with an optional file-descriptor number in front.
#: A redirection consumes the *target* token, which is a path rather than a
#: program, so both are skipped together.
_REDIRECTION = re.compile(r"\d*(?:>>|>|<<|<|&>>|&>)")

#: `2>&1` and friends: a descriptor duplication, which consumes no target.
_DESCRIPTOR_DUPLICATION = re.compile(r"\d*>&\d*")

#: Programs whose first non-flag argument is *another* program, mapped to the
#: flags that take a value (so the value is not mistaken for the program).
#:
#: ⚠️ The table is deliberately small and its default is conservative: a flag that
#: is not listed is treated as taking **no** value, so its argument is reported as
#: a program and the line is most likely refused — which is the safe direction.
#: Reading a value-taking flag as boolean would report the *value* as the program
#: and never check the real one.
_LAUNCHERS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "sudo": frozenset({"-u", "-g", "-p", "-C", "-h", "-r", "-t", "-U"}),
    "xargs": frozenset({"-n", "-I", "-L", "-s", "-P", "-d", "-a", "-E", "-i"}),
    "wsl": frozenset({"-d", "-u", "--distribution", "--user", "--cd", "--shell-type"}),
    "nice": frozenset({"-n"}),
    "time": frozenset({"-o", "-f"}),
    "nohup": frozenset(),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
}

#: Programs that run a line of their own when handed a `-c`-style flag.
_SHELLS = frozenset(
    {"sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh", "cmd", "powershell", "pwsh"}
)

#: The flags that make a shell run a nested line rather than start interactively.
_SHELL_EXEC_FLAGS = frozenset({"-c", "/c", "-command", "--command"})


def _split_segments(command: str) -> list[str]:
    """Split on the shell's separators, leaving quoted text alone.

    Also the place the unanswerable constructs are refused: an unquoted ``$(``,
    a backtick, a subshell parenthesis or an unbalanced quote all mean this line
    cannot be attributed to a set of programs, and saying so here keeps every
    caller from having to remember to.
    """
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            current.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(command):
                current.append(command[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < len(command):
            current.append(char)
            current.append(command[index + 1])
            index += 2
            continue
        if char in "$`()":
            raise UnparsableCommand(
                f"{command!r} contains {char!r}, so the programs it starts cannot be "
                f"read off the line"
            )
        if command.startswith("&&", index) or command.startswith("||", index):
            segments.append("".join(current))
            current = []
            index += 2
            continue
        if char in "|;":
            segments.append("".join(current))
            current = []
            index += 1
            continue
        if char == "&":
            # A *backgrounding* `&` separates commands; the `&` in `2>&1` does
            # not, and only the next character tells them apart.
            following = command[index + 1] if index + 1 < len(command) else ""
            if following in ("", " ", "\t"):
                segments.append("".join(current))
                current = []
                index += 1
                continue
        current.append(char)
        index += 1

    if quote:
        raise UnparsableCommand(f"{command!r} has an unbalanced {quote!r}")
    segments.append("".join(current))
    return segments


def _tokens(segment: str) -> list[str]:
    try:
        return split_command(segment)
    except ValueError as exc:  # shlex raises on an unbalanced quote
        raise UnparsableCommand(f"{segment!r} cannot be tokenised: {exc}") from exc


def _next_program(tokens: list[str], start: int) -> int:
    """Index of the next token that names a program, or ``len(tokens)``."""
    index = start
    while index < len(tokens):
        token = tokens[index]
        if _REDIRECTION.fullmatch(token):
            index += 2  # the operator and its target
            continue
        if _DESCRIPTOR_DUPLICATION.fullmatch(token):
            index += 1
            continue
        if _ASSIGNMENT.fullmatch(token):
            index += 1
            continue
        return index
    return index


def _launcher_target(tokens: list[str], value_flags: frozenset[str]) -> int | None:
    """Index of the program a launcher would start, or ``None`` if it names none.

    ``tokens`` are the launcher's own arguments (everything after its name).
    ``value_flags`` is the launcher's set of flags that consume the next token —
    without it, `sudo -u root git status` reports `root` as the program and never
    looks at `git`. A flag that is not in the set is read as taking no value.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _REDIRECTION.fullmatch(token):
            index += 2
            continue
        if _DESCRIPTOR_DUPLICATION.fullmatch(token):
            index += 1
            continue
        if _ASSIGNMENT.fullmatch(token):
            index += 1
            continue
        if token.startswith("-") and token != "-":
            if token.split("=", 1)[0] in value_flags and "=" not in token:
                index += 2
            else:
                index += 1
            continue
        return index
    return None


def _programs_in_segment(tokens: list[str], command: str) -> list[str]:
    index = _next_program(tokens, 0)
    if index >= len(tokens):
        return []

    found: list[str] = []
    while index < len(tokens):
        program = tokens[index]
        found.append(program)
        name = normalise_program(program)
        rest = tokens[index + 1 :]
        value_flags = _LAUNCHERS.get(name)
        if value_flags is not None:
            # `env python x.py` starts `python`, and the list should have to name
            # it. One level is enough: a launcher's target that is itself a
            # launcher is read as that launcher, which the list must then name.
            target = _launcher_target(rest, value_flags)
            if target is None:
                break
            index = index + 1 + target
            continue
        if name in _SHELLS and any(token.casefold() in _SHELL_EXEC_FLAGS for token in rest):
            raise UnparsableCommand(
                f"{command!r} hands a line of its own to {program!r}; that line is "
                f"not the line this check can see"
            )
        break
    return found


def programs(command: str) -> tuple[str, ...]:
    """Every program ``command`` would start, in the order it would start them.

    The reader is a command allow-list, so an unanswerable question must not read
    as a clean answer. :class:`UnparsableCommand` is raised for a line this cannot
    attribute to programs — command substitution, a variable or subshell in
    command position, a shell handed a nested line — and for a line with no
    program at all.

    It answers "which programs start", not "what those programs do": a listed
    ``git`` may still be asked to push, and a listed ``python`` may still be
    handed ``-c``. Arguments are the sandbox's business; this is only about what
    may be launched.
    """
    found: list[str] = []
    for segment in _split_segments(command):
        found.extend(_programs_in_segment(_tokens(segment), command))

    if not found:
        raise UnparsableCommand(
            f"{command!r} names no program that can be checked against a command list"
        )
    return tuple(found)
