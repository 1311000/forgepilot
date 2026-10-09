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

__all__ = ["likely_denial", "DENIAL_SIGNALS"]

#: ``128 + SIGSYS``. A process killed by an unhandled signal reports this, and
#: SIGSYS is what the kernel raises for a syscall seccomp refused — the one
#: signal nothing but a syscall filter produces.
SIGSYS_EXIT = 128 + 31

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
