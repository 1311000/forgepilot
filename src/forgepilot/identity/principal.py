"""Who is calling — service name ``principal``.

Ships against a token table rather than a real identity provider, so the
skeleton has an authentication boundary without needing SSO. The interface is the
part that matters: resolve an opaque credential to a :class:`Principal` plus the
:class:`Grant` that identity carries, or fail.

**A grant is not a decision.** It is what the identity provider says this person
*is* — their teams, the operations their role carries, the repositories they can
already reach. Whether a specific call is permitted is
:mod:`forgepilot.identity.policy`'s question, and keeping the two apart is what
stops "the IdP said they have access" from being mistaken for authorisation on a
particular resource in a particular request.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from forgepilot.identity.errors import AuthenticationFailed
from forgepilot.kernel import PluginService
from forgepilot.seams.refs import PrincipalId, RepoRef, TeamId, TenantId
from forgepilot.seams.security import NetworkAccess, Operation, Principal

__all__ = ["Grant", "TokenRecord", "PrincipalServicePlugin", "DEFAULT_TOKENS_ENV"]

SERVICE_NAME = "principal"


@dataclass(frozen=True, slots=True)
class Grant:
    """What the identity provider asserts about a principal."""

    operations: frozenset[Operation] = frozenset()
    repos: frozenset[RepoRef] = frozenset()
    repo_prefixes: frozenset[str] = frozenset()
    writable_paths: tuple[str, ...] = ()
    network: NetworkAccess = NetworkAccess.DENY


@dataclass(frozen=True, slots=True)
class TokenRecord:
    principal: Principal
    grant: Grant = field(default_factory=Grant)
    expires_at: datetime | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.expires_at


#: Environment variable holding a JSON array of dev credentials. Read by *name*
#: from config, so the tokens themselves never enter a profile file.
DEFAULT_TOKENS_ENV = "FORGEPILOT_DEV_TOKENS"


class PrincipalServicePlugin(PluginService):
    """Serves ``principal`` from an in-memory token table.

    Credentials come from two places, neither of which is a config file:

    - the ``tokens`` config key, for tests and fixtures;
    - the environment variable named by ``tokens_env``
      (default :data:`DEFAULT_TOKENS_ENV`), holding a JSON array.

    The second exists because without it there is no way to obtain a credential
    at runtime at all — the table would only ever be filled by tests, and the CLI
    would be unusable. Tokens are read from the environment rather than config for
    the same reason API keys are: a config file is committed and dumped.

    A real deployment replaces this provider with one that validates SSO tokens.
    The interface is what matters; the table is a stand-in.
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self.tokens_env: str = str(cfg.get("tokens_env", DEFAULT_TOKENS_ENV))
        self._tokens: dict[str, TokenRecord] = {}
        for entry in cfg.get("tokens") or []:
            self.add_token_from_mapping(entry)
        self.reload_from_env()

    # -- loading -----------------------------------------------------------
    def reload_from_env(self) -> int:
        """Load credentials from the environment. Returns how many were added.

        Malformed entries raise rather than being skipped: a typo in a credential
        table that silently yields no identity is much harder to diagnose than one
        that says what is wrong.
        """
        raw = os.environ.get(self.tokens_env)
        if not raw:
            return 0
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AuthenticationFailed(
                f"{self.tokens_env} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(entries, list):
            raise AuthenticationFailed(
                f"{self.tokens_env} must be a JSON array of credential objects"
            )

        added = 0
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise AuthenticationFailed(
                    f"{self.tokens_env}[{index}] must be an object"
                )
            self.add_token_from_mapping(entry)
            added += 1
        return added

    def add_token_from_mapping(self, entry: Mapping[str, Any]) -> TokenRecord:
        """Register a credential from a plain mapping."""
        token = str(entry.get("token", ""))
        if not token:
            raise AuthenticationFailed("a credential entry has no 'token'")

        principal = Principal(
            principal_id=PrincipalId(str(entry["principal_id"])),
            tenant_id=TenantId(str(entry.get("tenant_id", "default"))),
            team_ids=frozenset(TeamId(str(t)) for t in entry.get("teams") or ()),
            display_name=str(entry.get("display_name", "")),
        )
        grant = Grant(
            operations=frozenset(Operation(str(op)) for op in entry.get("operations") or ()),
            repos=frozenset(
                RepoRef(
                    host=str(r["host"]), namespace=str(r["namespace"]), name=str(r["name"])
                )
                for r in entry.get("repos") or ()
            ),
            repo_prefixes=frozenset(str(p) for p in entry.get("repo_prefixes") or ()),
            writable_paths=tuple(str(p) for p in entry.get("writable_paths") or ()),
            network=NetworkAccess(str(entry.get("network", "deny"))),
        )

        expires_at = entry.get("expires_at")
        return self.add_token(
            token,
            principal,
            grant,
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
        )

    # -- fixtures ----------------------------------------------------------
    def add_token(
        self,
        token: str,
        principal: Principal,
        grant: Grant | None = None,
        *,
        expires_at: datetime | None = None,
    ) -> TokenRecord:
        record = TokenRecord(principal=principal, grant=grant or Grant(), expires_at=expires_at)
        self._tokens[token] = record
        return record

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    # -- resolution --------------------------------------------------------
    def resolve(self, token: str) -> TokenRecord:
        """Resolve a credential, or raise.

        Unknown, empty and expired all produce the same error: the caller learns
        nothing about *why*, and there is no path where a missing token becomes an
        anonymous principal with default rights.
        """
        if not token:
            raise AuthenticationFailed("no credential supplied")
        record = self._tokens.get(token)
        if record is None:
            raise AuthenticationFailed("unknown credential")
        if record.is_expired():
            raise AuthenticationFailed("credential has expired")
        return record

    def principal_of(self, token: str) -> Principal:
        return self.resolve(token).principal


def principal_of(
    principal_id: str, tenant_id: str, teams: tuple[str, ...] = (), name: str = ""
) -> Principal:
    """Convenience constructor for fixtures and tests."""
    return Principal(
        principal_id=PrincipalId(principal_id),
        tenant_id=TenantId(tenant_id),
        team_ids=frozenset(TeamId(t) for t in teams),
        display_name=name,
    )
