"""Deriving :class:`ExecutionConstraints` from identity plus project policy.

Two independent inputs, intersected:

- the **grant** — what the identity provider says this person is,
- the **project policy** — what this repository or team permits.

Neither alone is authority. A person with broad rights working in a locked-down
project gets the project's limits; a narrow role in an open project gets the
role's. Intersection is the only operation that cannot widen either, which is why
it is the only one used.

The result is signed here, at the boundary, so that everything downstream can
tell a genuine set from a constructed one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from forgepilot.identity.principal import Grant
from forgepilot.identity.signing import sign
from forgepilot.seams.refs import RepoRef
from forgepilot.seams.security import (
    Budget,
    ExecutionConstraints,
    NetworkAccess,
    Operation,
    Principal,
)

__all__ = ["ProjectPolicy", "derive"]


@dataclass(frozen=True, slots=True)
class ProjectPolicy:
    """What a project permits, independent of who is asking.

    Defaulted to the *most restrictive* interpretation at every dimension:
    no repos, no operations, no writable paths, no network, the smallest budget.
    A project with no policy configured should not be an open project.
    """

    repos: frozenset[RepoRef] = frozenset()
    repo_prefixes: frozenset[str] = frozenset()
    allowed_operations: frozenset[Operation] = frozenset()
    writable_paths: tuple[str, ...] = ()
    network: NetworkAccess = NetworkAccess.DENY
    budget: Budget = Budget(
        max_tokens=200_000, max_tool_calls=200, max_wall_seconds=1800.0, max_cost_usd=10.0
    )

    @classmethod
    def open_project(
        cls,
        *,
        prefixes: frozenset[str] = frozenset({"*"}),
        operations: frozenset[Operation] = frozenset(Operation),
        writable_paths: tuple[str, ...] = ("src",),
        network: NetworkAccess = NetworkAccess.DENY,
        budget: Budget | None = None,
    ) -> "ProjectPolicy":
        """A permissive project, for fixtures and demos.

        Named ``open_project`` rather than being the default constructor, so that
        "this project permits a lot" is a visible choice at the call site.
        """
        return cls(
            repo_prefixes=prefixes,
            allowed_operations=operations,
            writable_paths=writable_paths,
            network=network,
            budget=budget or cls().budget,
        )


def derive(
    principal: Principal,
    grant: Grant,
    project: ProjectPolicy | None = None,
    *,
    key: bytes | None = None,
) -> ExecutionConstraints:
    """Intersect the grant with the project policy and sign the result.

    Passing ``project=None`` means "no project policy", which is read as the most
    restrictive one rather than as no restriction.
    """
    policy = project if project is not None else ProjectPolicy()

    constraints = ExecutionConstraints(
        principal_id=principal.principal_id,
        tenant_id=principal.tenant_id,
        # Repos are exact and operations are enum members, so set intersection is
        # the right operation for both.
        allowed_repos=grant.repos & policy.repos,
        permitted_operations=grant.operations & policy.allowed_operations,
        # Prefixes and paths are *patterns*, and patterns do not intersect as
        # sets: {"git.corp/team/*"} & {"*"} is the empty set, even though "*"
        # plainly permits the former. Intersecting them would silently strip
        # every prefix the moment a permissive policy met a specific grant —
        # which is the failure this function exists to prevent, inverted.
        allowed_repo_prefixes=_narrowed_prefixes(grant.repo_prefixes, policy.repo_prefixes),
        writable_paths=_narrowed_paths(grant.writable_paths, policy.writable_paths),
        network=(
            NetworkAccess.ALLOW
            if grant.network is NetworkAccess.ALLOW and policy.network is NetworkAccess.ALLOW
            else NetworkAccess.DENY
        ),
        # Budget comes from the project alone: an identity grant says what a
        # person may do, not how much of the company's spend they may consume.
        budget=policy.budget,
        issued_at=datetime.now(timezone.utc),
    )
    return sign(constraints, key)


# ---------------------------------------------------------------------------
# Pattern narrowing.
#
# A prefix is one of: ``"*"`` (anything), ``"literal/*"`` (a subtree), or
# ``"literal"`` (exactly). Narrowing keeps the grant's patterns that the policy
# permits — never the other way round, because the result must not be wider than
# either input.
# ---------------------------------------------------------------------------
def _prefix_shape(pattern: str) -> tuple[str, str]:
    if pattern == "*":
        return ("all", "")
    if pattern.endswith("/*"):
        return ("subtree", pattern[:-2])
    return ("exact", pattern)


def _prefix_covers(policy_pattern: str, grant_pattern: str) -> bool:
    """Whether every slug matching ``grant_pattern`` also matches ``policy_pattern``."""
    policy_kind, policy_value = _prefix_shape(policy_pattern)
    _, grant_value = _prefix_shape(grant_pattern)

    if policy_kind == "all":
        return True
    if policy_kind == "exact":
        return grant_value == policy_value and _prefix_shape(grant_pattern)[0] == "exact"
    # The policy covers a subtree; the grant must fall inside it.
    return grant_value == policy_value or grant_value.startswith(policy_value + "/")


def _narrowed_prefixes(
    grant: frozenset[str], policy: frozenset[str]
) -> frozenset[str]:
    if not policy:
        return frozenset()
    return frozenset(g for g in grant if any(_prefix_covers(p, g) for p in policy))


def _narrowed_paths(grant: tuple[str, ...], policy: tuple[str, ...]) -> tuple[str, ...]:
    """Keep grant paths the policy permits. Paths are directory prefixes here,
    matching how :meth:`ExecutionConstraints.allows_write_to` reads them."""
    if not policy:
        return ()
    kept = []
    for path in grant:
        normalised = path.strip("/")
        for allowed in policy:
            allowed_normalised = allowed.strip("/")
            if normalised == allowed_normalised or normalised.startswith(
                allowed_normalised + "/"
            ):
                kept.append(path)
                break
    return tuple(kept)
