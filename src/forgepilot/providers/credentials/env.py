"""The credential channel backed by the process environment — service ``credentials``.

**It is not an environment dump.** Only the names the profile declares are
reachable, and each one names the variable that holds it. A channel that resolved
any variable a caller asked for would hand the model the deployment's whole
environment through ``${...}``, which is a worse hole than the one this closes.

The value is read at resolve time, not at construction, so a deployment that loads
``.env.local`` after the plugin tree is built still works — the same ordering the
redaction salt relies on.

Why environment first: it is what this repository already uses for every other
secret (`.env.local`, loaded by `config/env_file.py`), and it is what the
deployment's process supervisor and CI already know how to inject. A
``git credential fill``- or vault-backed channel is the same seam with a different
``resolve`` — see `docs/notes/context-and-session.md` for what is not built.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.credentials import (
    SERVICE_NAME,
    CredentialRef,
    UnknownCredential,
)

__all__ = ["EnvCredentialPlugin"]


@dataclass(frozen=True, slots=True)
class _Entry:
    name: str
    env: str
    description: str


def _coerce_entries(raw: Any) -> dict[str, _Entry]:
    """Accept the long form and the shorthand, and refuse everything else loudly.

    A typo in this table would otherwise resolve to "no such secret" at the moment
    a command needs it, which reads like a wrong name rather than a wrong config.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(
            "credentials `entries` must be a mapping of name -> env var, or "
            "name -> {env, description}"
        )
    entries: dict[str, _Entry] = {}
    for name, spec in raw.items():
        if isinstance(spec, str):
            env, description = spec, ""
        elif isinstance(spec, Mapping):
            env = str(spec.get("env", ""))
            description = str(spec.get("description", ""))
        else:
            raise ValueError(
                f"credentials entry {name!r} must be an env var name or a mapping "
                f"with an `env` key, not {type(spec).__name__}"
            )
        if not env:
            raise ValueError(f"credentials entry {name!r} names no environment variable")
        entries[str(name)] = _Entry(name=str(name), env=env, description=description)
    return entries


class EnvCredentialPlugin(PluginService):
    """Serves ``credentials`` from the environment."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._entries = _coerce_entries(cfg.get("entries"))

    async def refs(self) -> tuple[CredentialRef, ...]:
        return tuple(
            CredentialRef(name=entry.name, description=entry.description)
            for entry in sorted(self._entries.values(), key=lambda e: e.name)
        )

    async def resolve(self, name: str) -> str:
        entry = self._entries.get(name)
        if entry is None:
            known = ", ".join(sorted(self._entries)) or "none"
            raise UnknownCredential(
                f"no secret is declared under the name {name!r}. Declared names: {known}."
            )
        value = os.environ.get(entry.env, "")
        if not value:
            # The name is configured and the value is not — different failure,
            # different fix, and worth saying which one this is.
            raise UnknownCredential(
                f"the secret {name!r} is declared but {entry.env} is empty in this "
                f"process. The declaration is fine; the environment is what is missing."
            )
        return value
