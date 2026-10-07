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

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from forgepilot.identity.errors import AuthenticationFailed
from forgepilot.kernel import PluginService
from forgepilot.seams.refs import PrincipalId, RepoRef, TeamId, TenantId
from forgepilot.seams.security import NetworkAccess, Operation, Principal

__all__ = ["Grant", "TokenRecord", "PrincipalServicePlugin"]

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
        return (now or datetime.now(timezone.utc)) >= self.expires_at


class PrincipalServicePlugin(PluginService):
    """Serves ``principal`` from an in-memory token table."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._tokens: dict[str, TokenRecord] = {}

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
