"""Turning a credential into a scoped request context.

This is the single place in ForgePilot where a credential becomes authority. It
is intentionally dull: resolve, intersect, sign, scope. Every interesting
question was answered elsewhere, and every answer is re-checked below.

Order matters. The signature is verified *inside* ``open_request_scope``, before
a context exists, so a forged or unsigned set never gets somewhere to live. And
the scope wraps the constraints rather than being derived from a bare context, so
"did anyone remember to establish authority for this request?" is answered by
whether the scope exists at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forgepilot.identity.constraints import ProjectPolicy, derive
from forgepilot.identity.principal import PrincipalServicePlugin
from forgepilot.identity.scope import RequestScope, open_request_scope
from forgepilot.seams.refs import PrincipalId, TenantId
from forgepilot.seams.security import Principal

__all__ = ["Credential", "RequestContext", "resolve_credential", "authenticate"]


@dataclass(frozen=True, slots=True)
class Credential:
    """Who is calling, and what they may do — before any scope exists.

    Separate from :class:`RequestContext` because the two have different
    lifetimes. A credential is checked **per request**; the scope it authorises
    may belong to something longer-lived than one request — a session, say, which
    outlives the POST that created it. Conflating the two would force a
    long-lived scope to be reopened per request, or a per-request one to be held
    open indefinitely.
    """

    principal: Principal
    constraints: Any
    project: ProjectPolicy


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Everything one authenticated request needs, and nothing more."""

    principal: Principal
    scope: RequestScope
    project: ProjectPolicy

    @property
    def constraints(self):
        return self.scope.constraints

    @property
    def ctx(self):
        """The scope's context. Use this to register request-scoped plugins."""
        return self.scope.ctx

    def close(self) -> None:
        self.scope.close()


def resolve_credential(
    root: Any,
    credential: str,
    *,
    project: ProjectPolicy | None = None,
    key: bytes | None = None,
) -> Credential:
    """Turn a credential into a principal and a signed authority set.

    No scope is opened, so this is also the right entry point for a caller that
    intends to hand the constraints to something that opens its own —
    ``runtime.assembly.assemble_session``, for instance, which opens the scope a
    session lives in and must open it *after* the session exists.

    An unknown or expired credential raises
    :class:`~forgepilot.identity.errors.AuthenticationFailed` rather than
    producing an anonymous principal — there is no default identity, which is why
    there is no way to "fall back" to one.
    """
    principals: PrincipalServicePlugin = root.principal
    record = principals.resolve(credential)

    policy = project if project is not None else ProjectPolicy()
    constraints = derive(record.principal, record.grant, policy, key=key)
    return Credential(principal=record.principal, constraints=constraints, project=policy)


def authenticate(
    root: Any,
    credential: str,
    *,
    project: ProjectPolicy | None = None,
    key: bytes | None = None,
    verify_signature: bool = True,
) -> RequestContext:
    """Authenticate, derive authority, and open a scope for one request."""
    cred = resolve_credential(root, credential, project=project, key=key)
    scope = open_request_scope(
        root, cred.constraints, key=key, verify_signature=verify_signature
    )
    return RequestContext(principal=cred.principal, scope=scope, project=cred.project)


def principal_id_for(value: str) -> PrincipalId:
    """Narrowing helper, kept so call sites do not import NewType constructors."""
    return PrincipalId(value)


def tenant_id_for(value: str) -> TenantId:
    return TenantId(value)
