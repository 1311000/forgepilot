"""Serving ``principal`` from an OIDC identity provider's token.

The shipped provider is a token table. Deployments do not have one; they have an
identity provider, and the row has to be replaced without touching the seam or
the 30-odd call sites that read ``root.principal.resolve(...)``. This is that
replacement.

**What it verifies, and what it deliberately does not.** The signature is checked
— an unsigned or tampered token is refused — with **HS256** and a shared secret
read by name from the environment, the same way every other credential in this
tree is read. It does not fetch a JWKS and it does not verify RS256: that needs a
crypto library this project does not depend on, and a provider that *appeared* to
verify RS256 while pinning nothing would be worse than one that says which
algorithm it can check. The refusal names the algorithm, so a deployment that
configured an RS256 client finds out at the first request instead of at the first
incident.

**A token is a credential, not an authorisation.** Everything the token asserts
becomes a :class:`~forgepilot.identity.principal.Grant` — what the IdP says this
person *is*. Whether a particular call is allowed is still
:mod:`forgepilot.identity.policy`'s decision, intersected with the project's
policy by :func:`forgepilot.identity.constraints.derive`. Granting an operation
here does not hand out the resource it operates on; the project still has to
allow it.

**Claims become rights only through configuration.** A token's ``groups`` claim
is a list of names; what a name *means* is the ``groups`` mapping below. A group
the mapping does not name contributes nothing — the alternative, inventing a
mapping from the group name, is how "everyone in ``backend`` can open pull
requests" becomes true without anyone deciding it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from forgepilot.config.errors import ConfigError
from forgepilot.identity.errors import AuthenticationFailed
from forgepilot.identity.principal import Grant, TokenRecord
from forgepilot.kernel import PluginService
from forgepilot.seams.refs import PrincipalId, RepoRef, TeamId, TenantId
from forgepilot.seams.security import NetworkAccess, Operation, Principal

__all__ = [
    "OidcPrincipalPlugin",
    "GroupRights",
    "decode_segment",
    "DEFAULT_SECRET_ENV",
]

SERVICE_NAME = "principal"

#: Environment variable holding the shared secret, read by *name* from config so
#: the secret itself never enters a profile file or a ``--dump-config``.
DEFAULT_SECRET_ENV = "FORGEPILOT_OIDC_SECRET"

_ALGORITHM = "HS256"


@dataclass(frozen=True, slots=True)
class GroupRights:
    """What one group name means, as configuration states it."""

    operations: frozenset[Operation] = frozenset()
    repos: frozenset[RepoRef] = frozenset()
    repo_prefixes: frozenset[str] = frozenset()
    writable_paths: tuple[str, ...] = ()
    commands: frozenset[str] | None = None
    network: NetworkAccess = NetworkAccess.DENY


def decode_segment(segment: str) -> bytes:
    """Decode one base64url JWT segment, padding included.

    Public because the tests build tokens with the same function the provider
    reads them with: a decoder only the provider uses is a decoder nobody
    cross-checks.
    """
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        raise AuthenticationFailed("malformed credential") from exc


def _json_object(raw: bytes, what: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AuthenticationFailed(f"malformed credential: the {what} is not JSON") from exc
    if not isinstance(value, dict):
        raise AuthenticationFailed(f"malformed credential: the {what} is not an object")
    return value


def _repo_ref(text: str) -> RepoRef:
    parts = text.split("/")
    if len(parts) != 3 or not all(parts):
        raise ConfigError(
            f"{text!r} is not a repository: a repository is 'host/namespace/name', "
            "with all three parts non-empty"
        )
    return RepoRef(host=parts[0], namespace=parts[1], name=parts[2])


def _operations(value: Any, *, where: str) -> frozenset[Operation]:
    if value is None:
        return frozenset()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ConfigError(f"{where} must be a list of operations, not {value!r}")
    operations: set[Operation] = set()
    for name in value:
        try:
            operations.add(Operation(str(name)))
        except ValueError as exc:
            known = ", ".join(member.value for member in Operation)
            raise ConfigError(
                f"{where} names {name!r}, which is not an operation. Known: {known}."
            ) from exc
    return frozenset(operations)


def _strings(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ConfigError(f"{where} must be a list of strings, not {value!r}")
    return tuple(str(item) for item in value)


def _group_rights(name: str, raw: Any) -> GroupRights:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"groups[{name!r}] must be a mapping of rights, not {raw!r}")
    where = f"groups[{name!r}]"
    commands: frozenset[str] | None = None
    if raw.get("commands") is not None:
        commands = frozenset(_strings(raw.get("commands"), where=f"{where}.commands"))
    return GroupRights(
        operations=_operations(raw.get("operations"), where=f"{where}.operations"),
        repos=frozenset(
            _repo_ref(text) for text in _strings(raw.get("repos"), where=f"{where}.repos")
        ),
        repo_prefixes=frozenset(_strings(raw.get("repo_prefixes"), where=f"{where}.repo_prefixes")),
        writable_paths=_strings(raw.get("writable_paths"), where=f"{where}.writable_paths"),
        commands=commands,
        network=NetworkAccess(str(raw.get("network", "deny"))),
    )


class OidcPrincipalPlugin(PluginService):
    """Serves ``principal`` by validating an OIDC identity token.

    Configuration (every key, and the refusal for a missing one):

    - ``issuer`` (required) — the ``iss`` claim must equal it exactly;
    - ``audience`` (required) — the ``aud`` claim must contain it;
    - ``secret_env`` (default :data:`DEFAULT_SECRET_ENV`) — *name* of the env var
      holding the HS256 shared secret;
    - ``principal_claim`` (default ``preferred_username``, then ``sub``);
    - ``tenant_claim`` (default ``tenant``) with ``default_tenant`` (``default``);
    - ``teams_claim`` (default ``groups``) — the names mapped by ``groups``;
    - ``clock_skew_s`` (default 60) — applied to ``exp`` and ``nbf``;
    - ``groups`` — group name to :class:`GroupRights`.

    ``resolve`` is synchronous because the seam's callers are: it verifies a
    signature against a secret already in memory, so it needs no I/O.
    """

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        for required in ("issuer", "audience"):
            if not cfg.get(required):
                raise ConfigError(
                    f"the oidc principal provider needs {required!r}: it is what the "
                    f"token's {required[:3]!r} claim is checked against, and a provider "
                    "that accepts any value checks nothing"
                )
        self.issuer: str = str(cfg["issuer"])
        self.audience: str = str(cfg["audience"])
        self.secret_env: str = str(cfg.get("secret_env", DEFAULT_SECRET_ENV))
        self.principal_claim: str = str(cfg.get("principal_claim", "preferred_username"))
        self.tenant_claim: str = str(cfg.get("tenant_claim", "tenant"))
        self.default_tenant: str = str(cfg.get("default_tenant", "default"))
        self.teams_claim: str = str(cfg.get("teams_claim", "groups"))
        self.clock_skew_s: float = float(cfg.get("clock_skew_s", 60))
        raw_groups = cfg.get("groups") or {}
        if not isinstance(raw_groups, Mapping):
            raise ConfigError("groups must be a mapping of group name to rights")
        self.groups: dict[str, GroupRights] = {
            str(name): _group_rights(str(name), rights) for name, rights in raw_groups.items()
        }
        self._secret = self._read_secret()

    def _read_secret(self) -> bytes:
        raw = os.environ.get(self.secret_env)
        if not raw:
            raise ConfigError(
                f"the oidc principal provider needs {self.secret_env} to hold the HS256 "
                "secret it verifies tokens with. It is read by name rather than from "
                "config because a config file is committed and dumped."
            )
        return raw.encode("utf-8")

    # -- resolution --------------------------------------------------------
    def resolve(self, token: str) -> TokenRecord:
        """Verify a token and return what it asserts, or raise.

        Every failure is an :class:`AuthenticationFailed`; the messages differ
        because a deployment's operator needs to know *which* check failed, and
        they are only ever logged — the HTTP boundary answers 401 with none of
        this text, so a caller learns nothing about why.
        """
        if not token:
            raise AuthenticationFailed("no credential supplied")
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthenticationFailed("malformed credential: an identity token has three parts")
        header = _json_object(decode_segment(parts[0]), "header")
        algorithm = header.get("alg")
        if algorithm != _ALGORITHM:
            raise AuthenticationFailed(
                f"unsupported signing algorithm {algorithm!r}: this provider verifies "
                f"{_ALGORITHM} only, because that is the one it can check without a "
                "crypto library. Configure the identity provider's client for HS256, or "
                "add a JWKS-verifying provider before relying on this one."
            )
        expected = hmac.new(
            self._secret,
            f"{parts[0]}.{parts[1]}".encode("ascii", "strict"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, decode_segment(parts[2])):
            raise AuthenticationFailed("the identity token's signature does not verify")

        claims = _json_object(decode_segment(parts[1]), "payload")
        expires_at = self._verified_times(claims)
        self._verified_issuer(claims)
        self._verified_audience(claims)

        principal_id = self._principal_id(claims)
        teams = self._teams(claims)
        grant = self._grant(teams, claims)
        principal = Principal(
            principal_id=principal_id,
            tenant_id=TenantId(str(claims.get(self.tenant_claim) or self.default_tenant)),
            team_ids=teams,
            display_name=str(claims.get("name", "")),
        )
        return TokenRecord(principal=principal, grant=grant, expires_at=expires_at)

    def _verified_times(self, claims: Mapping[str, Any]) -> datetime:
        now = datetime.now(UTC).timestamp()
        expires = claims.get("exp")
        if not isinstance(expires, int | float):
            raise AuthenticationFailed(
                "the identity token has no numeric 'exp': a token without an expiry is "
                "a credential that never stops working"
            )
        if now > float(expires) + self.clock_skew_s:
            raise AuthenticationFailed("credential has expired")
        not_before = claims.get("nbf")
        if isinstance(not_before, int | float) and now < float(not_before) - self.clock_skew_s:
            raise AuthenticationFailed("credential is not valid yet")
        return datetime.fromtimestamp(float(expires), UTC)

    def _verified_issuer(self, claims: Mapping[str, Any]) -> None:
        if str(claims.get("iss", "")) != self.issuer:
            raise AuthenticationFailed(
                f"the identity token was issued by {claims.get('iss')!r}, not {self.issuer!r}"
            )

    def _verified_audience(self, claims: Mapping[str, Any]) -> None:
        audience = claims.get("aud")
        if isinstance(audience, str):
            audiences = (audience,)
        elif isinstance(audience, Sequence):
            audiences = tuple(str(item) for item in audience)
        else:
            audiences = ()
        if self.audience not in audiences:
            raise AuthenticationFailed(
                f"the identity token is for {list(audiences)!r}, not {self.audience!r}"
            )

    def _principal_id(self, claims: Mapping[str, Any]) -> PrincipalId:
        value = claims.get(self.principal_claim)
        if not value and self.principal_claim != "sub":
            value = claims.get("sub")
        if not value:
            raise AuthenticationFailed(
                f"the identity token names no principal: it has no {self.principal_claim!r} "
                "claim and no 'sub'"
            )
        return PrincipalId(str(value))

    def _teams(self, claims: Mapping[str, Any]) -> frozenset[TeamId]:
        raw = claims.get(self.teams_claim)
        if raw is None:
            return frozenset()
        if isinstance(raw, str):
            names: tuple[str, ...] = (raw,)
        elif isinstance(raw, Sequence):
            names = tuple(str(item) for item in raw)
        else:
            raise AuthenticationFailed(
                f"the identity token's {self.teams_claim!r} claim is not a list of groups"
            )
        return frozenset(TeamId(name) for name in names)

    def _grant(self, teams: frozenset[TeamId], claims: Mapping[str, Any]) -> Grant:
        """Union the rights of every group the token names, and nothing else."""
        operations: set[Operation] = set()
        repos: set[RepoRef] = set()
        prefixes: set[str] = set()
        writable: list[str] = []
        commands: frozenset[str] | None = None
        network = NetworkAccess.DENY
        for team in teams:
            rights = self.groups.get(str(team))
            if rights is None:
                # A group this deployment never mapped grants nothing. Inventing
                # rights from a name would make membership itself an authorisation.
                continue
            operations |= rights.operations
            repos |= rights.repos
            prefixes |= rights.repo_prefixes
            for path in rights.writable_paths:
                if path not in writable:
                    writable.append(path)
            if rights.commands is not None:
                commands = rights.commands if commands is None else commands | rights.commands
            if rights.network is NetworkAccess.ALLOW:
                network = NetworkAccess.ALLOW
        return Grant(
            operations=frozenset(operations),
            repos=frozenset(repos),
            repo_prefixes=frozenset(prefixes),
            writable_paths=tuple(writable),
            commands=commands,
            network=network,
        )

    def principal_of(self, token: str) -> Principal:
        return self.resolve(token).principal
