"""Was the *boundary* the reason, or was it the command?

The distinction is not cosmetic. Codex issue #41320 documents what happens when
the sandbox's role is dropped from the model-facing result: the model receives
only an exit code, cannot tell "refused by the boundary" from "the code is
broken", and responds by looking for another execution environment rather than
taking the escalation path it was given. An agent that cannot see the wall keeps
walking into it.

**Deliberately conservative, and deliberately says "likely".** The signals below
are not proof. ``permission denied`` is also what a command says when a file is
simply not yours; ``127`` is `command not found` *and* a plausible sandbox
outcome. A classifier that claimed certainty here would be worse than none,
because a wrong "the sandbox did that" sends the model down the escalation path
for a genuine bug.

Two levels exist, and they must not be blurred:

- **Certain** — ``ExecResult.executed`` is ``False``. Nothing ran, and the
  provider's own ``stderr`` says why. That is a fact, not an inference, and this
  module stays out of it.
- **Likely** — everything here. A command ran, and something about how it failed
  is characteristic of a boundary rather than of the program.

Matching Codex's ``is_likely_sandbox_denied`` in spirit; narrower in practice.
"""

from __future__ import annotations

from forgepilot.seams.sandbox import ExecResult

__all__ = [
    "DENIAL_SIGNALS",
    "NOT_FOUND",
    "SIGSYS_EXIT",
    "likely_denial",
    "not_executed_reason",
]

#: ``128 + SIGSYS``. A process killed by an unhandled signal reports this, and
#: SIGSYS is what the kernel raises for a syscall seccomp refused — the one
#: signal nothing but a syscall filter produces.
SIGSYS_EXIT = 128 + 31

#: Conventional shell status for "command not found". It is the discriminator
#: between the two ways a command can be *not executed*, which need opposite
#: answers from whoever reads the result.
NOT_FOUND = 127

#: ``(needle in stderr, what to tell the model)``. Lower-cased matching.
DENIAL_SIGNALS: tuple[tuple[str, str], ...] = (
    ("read-only file system", "the filesystem is read-only inside the sandbox"),
    ("operation not permitted", "the sandbox refused a permission"),
    (
        "permission denied",
        "a permission was denied — the sandbox, or the file's own permissions",
    ),
)


def likely_denial(result: ExecResult) -> str:
    """A reason the boundary probably refused, or ``''`` if nothing suggests it.

    Returns a phrase describing the *signal*, not a verdict: the caller is
    expected to relay it as a possibility. An empty string is the common and
    correct answer for an ordinary failing command.
    """
    if not result.executed:
        # Already certain, and already explained by whoever refused it.
        return ""
    if result.exit_code == 0:
        return ""

    if result.exit_code == SIGSYS_EXIT:
        return (
            "the process was killed by SIGSYS, which means a syscall was blocked — only "
            "a sandbox does that"
        )

    haystack = f"{result.stderr}\n{result.stdout}".lower()
    for needle, explanation in DENIAL_SIGNALS:
        if needle in haystack:
            return explanation
    return ""


def not_executed_reason(result: ExecResult) -> str:
    """Why nothing ran, when ``executed`` is ``False``.

    This is the *certain* half of the module — no guessing, because the provider
    has already said what happened. But it is not one case, it is two, and they
    call for opposite responses:

    - **The executable was not there.** ``127``, and the provider's own stderr
      says so. Nothing about the host is wrong; the command is. Fix the command.
    - **The provider does not execute at all.** A recording sandbox, or a host
      with no backend. There is no command to fix, and retrying differently is
      wasted effort.

    Telling them apart is the whole job. The message this replaced named the
    second in both cases, and a real model — told "the configured sandbox records
    calls without running them" when in fact the harness had mangled its own argv
    — concluded that execution was impossible on this host and stopped running
    anything. That is Codex #41320 with the blame on the wrong party: the model
    saw a wall, and it was not the wall it was standing at.
    """
    detail = result.stderr.strip() or "the sandbox gave no reason"
    argv0 = result.argv[0] if result.argv else "?"
    head = (
        f"exit: {result.exit_code}   enforcement: {result.enforcement.value!r}\n"
        f"argv: {list(result.argv)}\n"
        f"reason given: {detail}"
    )

    if result.exit_code == NOT_FOUND:
        return (
            f"{head}\n"
            f"Nothing was spawned: {argv0!r} could not be found. That is a problem "
            f"with the command, not with the sandbox — check the path and the spelling, "
            f"and try a command that does not depend on it."
        )

    return (
        f"{head}\n"
        f"The sandbox provider in use may not execute commands at all. If every command "
        f"reports this, say so plainly rather than looking for another way to run things "
        f"— on a host with no executing backend there is none."
    )
