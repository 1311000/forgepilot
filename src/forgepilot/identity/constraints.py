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
from datetime import UTC, datetime

from forgepilot.identity.principal import Grant
from forgepilot.identity.signing import sign
from forgepilot.seams.refs import RepoRef
from forgepilot.seams.security import (
    Budget,
    ExecutionConstraints,
    NetworkAccess,
    Operation,
    Principal,
    narrow_commands,
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
    #: Programs the project permits starting. ``None`` (the default) means the
    #: project states no list, which is **not** the same as an empty list.
    commands: frozenset[str] | None = None
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
    ) -> ProjectPolicy:
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
        # Commands are program names, so they intersect as sets — except that
        # "no list declared" (None) is the identity here, not the empty set. An
        # unstated grant must not erase a stated project list, and vice versa.
        allowed_commands=narrow_commands(grant.commands, policy.commands),
        network=(
            NetworkAccess.ALLOW
            if grant.network is NetworkAccess.ALLOW and policy.network is NetworkAccess.ALLOW
            else NetworkAccess.DENY
        ),
        # Budget comes from the project alone: an identity grant says what a
        # person may do, not how much of the company's spend they may consume.
        budget=policy.budget,
        issued_at=datetime.now(UTC),
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


def _path_rule(value: str) -> tuple[str, str]:
    """``("all", "")`` for a rule that names everything, else ``("subtree", path)``.

    ``"."`` and ``""`` are how a grant says *the whole checkout* — the shipped
    example uses ``"."`` on both sides — and :meth:`allows_write_to` reads a rule
    as "this path, or anything under it".
    """
    cleaned = value.replace("\\", "/").strip().strip("/")
    if cleaned in ("", "."):
        return ("all", "")
    return ("subtree", cleaned)


def _intersect_paths(grant: str, policy: str) -> str | None:
    """The narrower of two writable-path rules, or ``None`` when they are disjoint.

    **Narrowing paths is an intersection, not a filter**, and writing it as a
    filter is a mistake with two symmetric symptoms:

        grant "."   policy "src"  ->  nothing writable   (should be "src")
        grant "src" policy "."    ->  nothing writable   (should be "src")

    The old rule kept a grant path only when it already fell inside a policy path,
    so a grant *wider* than the policy matched nothing — and a grant *narrower*
    than a permissive policy matched nothing either. It happened to work in every
    existing test because every one of them used the **same** set on both sides,
    which is the one case a filter and an intersection agree on.

    Found by an eval task whose point was the opposite: a project restricted to
    `src/` behind a principal granted everything, and the guard's refusal read
    *"nothing is writable"* — a true statement about the intersection the code
    computed and a false one about the authority the caller held.
    """
    grant_kind, grant_value = _path_rule(grant)
    policy_kind, policy_value = _path_rule(policy)

    if grant_kind == "all":
        return policy
    if policy_kind == "all":
        return grant
    if grant_value == policy_value:
        return grant
    if grant_value.startswith(policy_value + "/"):
        return grant  # the grant is the narrower one
    if policy_value.startswith(grant_value + "/"):
        return policy  # the policy is
    return None


def _narrowed_paths(grant: tuple[str, ...], policy: tuple[str, ...]) -> tuple[str, ...]:
    """The intersection of two writable-path rule sets."""
    if not policy:
        return ()
    kept: list[str] = []
    for path in grant:
        for allowed in policy:
            winner = _intersect_paths(path, allowed)
            if winner is not None:
                kept.append(winner)
    return tuple(dict.fromkeys(kept))
