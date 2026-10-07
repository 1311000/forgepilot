"""The identity/permission carrier.

:class:`ExecutionConstraints` is the single object that carries a caller's
authority from the HTTP boundary all the way down into individual tools. The
architecture depends on that propagation being non-optional: a tool must not be
able to reach a repository, a path, or an operation that the constraints do not
permit, and a subsystem that never received constraints must fail closed rather
than default to permissive.

The fail-closed property is provided by the kernel's ``inject`` gating (a plugin
whose declared dependency is unavailable never activates) rather than by anything
in this module — see ``CLAUDE.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from forgepilot.seams.refs import PrincipalId, RepoRef, TeamId, TenantId

__all__ = [
    "Operation",
    "Principal",
    "Budget",
    "NetworkAccess",
    "ExecutionConstraints",
    "normalise_relative",
]


class Operation(StrEnum):
    """An action a principal may be permitted to perform.

    Coarse on purpose: these are the decisions a policy makes, not individual
    tool names. A tool maps to one of these; it does not invent its own.
    """

    READ_CODE = "read_code"
    WRITE_CODE = "write_code"
    RUN_TESTS = "run_tests"
    OPEN_PR = "open_pr"
    READ_TICKET = "read_ticket"
    COMMENT_TICKET = "comment_ticket"
    SPAWN_SUBAGENT = "spawn_subagent"


class NetworkAccess(StrEnum):
    DENY = "deny"
    ALLOW = "allow"


@dataclass(frozen=True, slots=True)
class Budget:
    """Hard limits enforced as monotonic guards, never as advisory hints."""

    max_tokens: int = 200_000
    max_tool_calls: int = 200
    max_wall_seconds: float = 1800.0
    max_cost_usd: float = 10.0


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller."""

    principal_id: PrincipalId
    tenant_id: TenantId
    team_ids: frozenset[TeamId] = frozenset()
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionConstraints:
    """What a specific request is allowed to do. Frozen by construction.

    Deliberately carries **no** ``signature`` field yet. The design calls for an
    HMAC so a plugin cannot forge constraints, but signing and verification are
    part of the identity layer (M5). Shipping an unverified signature field now
    would be worse than shipping none: it would look like tamper protection while
    providing nothing.

    Construct via :meth:`for_principal` rather than by hand — the defaults are
    intentionally the most restrictive ones.
    """

    principal_id: PrincipalId
    tenant_id: TenantId
    allowed_repos: frozenset[RepoRef] = frozenset()
    #: Slug prefixes, e.g. ``"git.corp/team/*"``. Matched against RepoRef.slug.
    allowed_repo_prefixes: frozenset[str] = frozenset()
    permitted_operations: frozenset[Operation] = frozenset()
    #: Paths writable, relative to the workspace root. Empty means "none".
    writable_paths: tuple[str, ...] = ()
    network: NetworkAccess = NetworkAccess.DENY
    budget: Budget = field(default_factory=Budget)
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    #: HMAC over the canonical payload, set by :func:`forgepilot.identity.signing.sign`.
    #:
    #: Empty means **unsigned**, and nothing should trust it. The field exists now
    #: because verification does; an unverified signature would be worse than none,
    #: since it would look like tamper protection while providing none.
    signature: str = ""

    @property
    def is_signed(self) -> bool:
        return bool(self.signature)

    def canonical_payload(self) -> dict[str, Any]:
        """Deterministic representation of everything the signature covers.

        Everything *except* ``signature`` itself, with every collection sorted, so
        that two constraint sets that mean the same thing serialise identically —
        otherwise a legitimate re-serialisation would look like tampering.
        """
        return {
            "principal_id": str(self.principal_id),
            "tenant_id": str(self.tenant_id),
            "allowed_repos": sorted(repo.slug for repo in self.allowed_repos),
            "allowed_repo_prefixes": sorted(self.allowed_repo_prefixes),
            "permitted_operations": sorted(op.value for op in self.permitted_operations),
            "writable_paths": sorted(self.writable_paths),
            "network": str(self.network),
            "budget": {
                "max_tokens": self.budget.max_tokens,
                "max_tool_calls": self.budget.max_tool_calls,
                "max_wall_seconds": self.budget.max_wall_seconds,
                "max_cost_usd": self.budget.max_cost_usd,
            },
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def for_principal(
        cls,
        principal: Principal,
        *,
        allowed_repos: frozenset[RepoRef] = frozenset(),
        allowed_repo_prefixes: frozenset[str] = frozenset(),
        permitted_operations: frozenset[Operation] = frozenset(),
        writable_paths: tuple[str, ...] = (),
        network: NetworkAccess = NetworkAccess.DENY,
        budget: Budget | None = None,
    ) -> "ExecutionConstraints":
        """Build constraints for ``principal``.

        Note there is no path from a model-produced value (a ticket
        understanding, a classification, a plan) into this constructor. The
        decision function's *signature* is what enforces "the local model
        proposes but never decides", not a convention.
        """
        return cls(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            allowed_repos=allowed_repos,
            allowed_repo_prefixes=allowed_repo_prefixes,
            permitted_operations=permitted_operations,
            writable_paths=writable_paths,
            network=network,
            budget=budget or Budget(),
        )

    # -- decisions ---------------------------------------------------------
    def allows_repo(self, repo: RepoRef) -> bool:
        """Whether ``repo`` is reachable at all under these constraints."""
        if repo in self.allowed_repos:
            return True
        slug = repo.slug
        return any(
            _prefix_matches(prefix, slug) for prefix in self.allowed_repo_prefixes
        )

    def allows_operation(self, operation: Operation) -> bool:
        return operation in self.permitted_operations

    def allows_write_to(self, relative_path: str) -> bool:
        """Whether ``relative_path`` (from the workspace root) may be modified.

        Compared after normalisation so that ``./a/../b`` cannot slip past a rule
        written for ``b``.
        """
        if not self.writable_paths:
            return False
        candidate = normalise_relative(relative_path)
        if candidate is None:
            return False
        for allowed in self.writable_paths:
            allowed_norm = normalise_relative(allowed)
            if allowed_norm is None:
                continue
            if candidate == allowed_norm or candidate.startswith(allowed_norm.rstrip("/") + "/"):
                return True
        return False

    def narrowed_to(self, other: "ExecutionConstraints") -> "ExecutionConstraints":
        """Return constraints that are ``self`` intersected with ``other``.

        A subagent, a resumed session, or a forked session must only ever be able
        to *narrow* authority. Intersection is the operation that guarantees that
        structurally, rather than relying on callers remembering to check.

        **The signature is dropped**, because the content changed. Keeping it would
        mean a narrowed set carrying a signature over the wider one — the exact
        forgery the signature exists to prevent. The result must be re-signed by
        whoever narrowed it.
        """
        if other.principal_id != self.principal_id:
            raise ValueError(
                "cannot narrow across principals: "
                f"{self.principal_id!r} vs {other.principal_id!r}"
            )
        return ExecutionConstraints(
            principal_id=self.principal_id,
            tenant_id=self.tenant_id,
            allowed_repos=self.allowed_repos & other.allowed_repos,
            allowed_repo_prefixes=self.allowed_repo_prefixes & other.allowed_repo_prefixes,
            permitted_operations=self.permitted_operations & other.permitted_operations,
            writable_paths=tuple(
                p for p in self.writable_paths if other.allows_write_to(p)
            ),
            network=(
                NetworkAccess.ALLOW
                if self.network is NetworkAccess.ALLOW and other.network is NetworkAccess.ALLOW
                else NetworkAccess.DENY
            ),
            budget=_min_budget(self.budget, other.budget),
            issued_at=self.issued_at,
            expires_at=_earliest(self.expires_at, other.expires_at),
        )


def _prefix_matches(prefix: str, slug: str) -> bool:
    """Match a glob-ish repo prefix: ``a/b/*`` matches ``a/b/c``, and a bare
    ``a/b`` matches only ``a/b`` exactly."""
    if prefix.endswith("/*"):
        return slug.startswith(prefix[:-1])
    if prefix.endswith("*"):
        return slug.startswith(prefix[:-1])
    return slug == prefix


def normalise_relative(path: str) -> str | None:
    """Normalise a workspace-relative path, rejecting escapes and absolutes."""
    cleaned = path.replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/") or ":" in cleaned.split("/")[0]:
        return None  # absolute, drive-qualified, or empty
    parts: list[str] = []
    for part in cleaned.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None  # escapes the workspace root
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) or None


def _min_budget(a: Budget, b: Budget) -> Budget:
    return Budget(
        max_tokens=min(a.max_tokens, b.max_tokens),
        max_tool_calls=min(a.max_tool_calls, b.max_tool_calls),
        max_wall_seconds=min(a.max_wall_seconds, b.max_wall_seconds),
        max_cost_usd=min(a.max_cost_usd, b.max_cost_usd),
    )


def _earliest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)
