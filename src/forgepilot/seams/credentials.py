"""The credential channel: names for secrets the model may *use* but never hold.

A secret that never enters the context cannot be leaked by the context. That is
the whole design, and it has one awkward consequence: the agent still has to be
able to run a command that needs the secret. The channel is how both facts hold
at once.

The model is told the **names** and writes ``${NAME}`` where the value belongs.
The value is substituted at the last possible moment — after approval, after the
guards, in the arm phase's shadow — by the one component allowed to read it. The
log records what the model wrote (the placeholder), never what the process ran.

Two things make this a mechanism rather than a convention:

- **The placeholder is refused if it cannot be resolved.** An unknown name is a
  refusal in ``arm``, not an empty string, because a shell line with an empty
  secret in it usually fails *somewhere else* and looks like something else.
- **A secret-shaped literal is refused outright** (see
  ``runtime.tools.arm.LiteralSecretRefused``). Without that, a model that pastes
  the token straight into the command bypasses the channel entirely — and a model
  *will* paste it, because that is what the token looks like in its training data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from forgepilot.errors import ForgePilotError

__all__ = [
    "SERVICE_NAME",
    "PLACEHOLDER_PATTERN",
    "CredentialChannel",
    "CredentialError",
    "CredentialRef",
    "UnknownCredential",
    "placeholder",
    "placeholders",
]

SERVICE_NAME = "credentials"

#: ``${NAME}`` — what a model writes where a secret belongs.
#:
#: Deliberately the shell's own syntax. The command is a shell line, so a form the
#: shell *would* have expanded is the one a model reaches for untaught, and it
#: survives quoting (``--token "${TOKEN}"`` keeps working) where a bare ``$NAME``
#: would not. It is also unambiguous enough to refuse safely: text containing
#: ``${...}`` is either ours or a shell parameter expansion, and both want the same
#: thing done with them.
PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class CredentialError(ForgePilotError):
    """The credential channel could not do what was asked of it."""


class UnknownCredential(CredentialError):
    """No secret is held under that name.

    Raised rather than resolving to ``""``. An empty secret turns into a request
    that fails elsewhere, with an error that points at the wrong thing.
    """


@dataclass(frozen=True, slots=True)
class CredentialRef:
    """What the model may *know* about a secret: that it exists, and what for.

    Never the value — this is the object that goes into a prompt.
    """

    name: str
    description: str = ""


@runtime_checkable
class CredentialChannel(Protocol):
    """Where ``${NAME}`` is resolved. Service name :data:`SERVICE_NAME`."""

    async def refs(self) -> tuple[CredentialRef, ...]:
        """The names this channel can resolve, for the prompt and diagnostics."""
        ...

    async def resolve(self, name: str) -> str:
        """The value. Raises :class:`UnknownCredential` if it cannot.

        Async because a real deployment's implementation shells out (`git
        credential fill`, a vault CLI, a metadata service); an env-backed one
        simply has nothing to await.
        """
        ...


def placeholder(name: str) -> str:
    return "${" + name + "}"


def placeholders(text: str) -> tuple[str, ...]:
    """The names referenced in ``text``, de-duplicated, in first-use order."""
    seen: dict[str, None] = {}
    for match in PLACEHOLDER_PATTERN.finditer(text or ""):
        seen.setdefault(match.group(1), None)
    return tuple(seen)
