"""Identity, signing, and the propagation of authority into a request scope.

The assertion this file exists for is
:func:`test_a_plugin_needing_constraints_cannot_load_outside_a_scope`: it is the
difference between "we try to pass constraints everywhere" and "a plugin that
needs authority cannot exist where authority does not".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from forgepilot.config import resolve_plugin
from forgepilot.identity import (
    AuthenticationFailed,
    Grant,
    PolicyDecision,
    PolicyServicePlugin,
    ProjectPolicy,
    RequestScope,
    SignatureInvalid,
    SigningKeyMissing,
    derive,
    load_key,
    open_request_scope,
    sign,
    verify,
)
from forgepilot.identity.errors import IdentityError
from forgepilot.identity.principal import PrincipalServicePlugin, principal_of
from forgepilot.identity.policy import ResourceRef
from forgepilot.httpapi.deps import authenticate
from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.seams.refs import PrincipalId, RepoRef, TenantId
from forgepilot.seams.security import (
    Budget,
    ExecutionConstraints,
    NetworkAccess,
    Operation,
    Principal,
)
from forgepilot.seams.retriever import SearchQuery

KEY = b"test-signing-key"
REPO_A = RepoRef(host="git.corp", namespace="team", name="svc-a")
REPO_B = RepoRef(host="git.corp", namespace="other", name="secret")


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------
def _bare(**overrides) -> ExecutionConstraints:
    base = dict(
        principal_id=PrincipalId("alice"),
        tenant_id=TenantId("acme"),
        permitted_operations=frozenset({Operation.READ_CODE}),
    )
    base.update(overrides)
    return ExecutionConstraints(**base)


def test_sign_then_verify_round_trips() -> None:
    signed = sign(_bare(), KEY)

    assert signed.is_signed
    verify(signed, KEY)  # does not raise


def test_tampering_after_signing_is_detected() -> None:
    """The whole point: widening a set after the fact is detectable."""
    from dataclasses import replace

    signed = sign(_bare(), KEY)
    widened = replace(signed, permitted_operations=frozenset(Operation))

    with pytest.raises(SignatureInvalid, match="do not match their signature"):
        verify(widened, KEY)


def test_an_unsigned_set_is_refused() -> None:
    """Refused, not "unverified but probably fine" — or omitting it would be the way past."""
    with pytest.raises(SignatureInvalid, match="unsigned"):
        verify(_bare(), KEY)


def test_a_different_key_does_not_verify() -> None:
    with pytest.raises(SignatureInvalid):
        verify(sign(_bare(), KEY), b"another-key")


def test_narrowing_drops_the_signature() -> None:
    """A narrowed set carries different content, so the old signature must not survive.

    Keeping it would mean a narrower set carrying a signature over a wider one —
    exactly the forgery signing exists to catch.
    """
    wide = sign(_bare(permitted_operations=frozenset(Operation)), KEY)
    narrow = wide.narrowed_to(_bare(permitted_operations=frozenset({Operation.READ_CODE})))

    assert narrow.is_signed is False
    with pytest.raises(SignatureInvalid, match="unsigned"):
        verify(narrow, KEY)


def test_missing_key_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("FORGEPILOT_SIGNING_KEY", raising=False)

    with pytest.raises(SigningKeyMissing, match="FORGEPILOT_SIGNING_KEY"):
        load_key()


def test_canonical_form_is_order_independent() -> None:
    """Two sets that mean the same thing must sign identically."""
    a = _bare(
        permitted_operations=frozenset({Operation.READ_CODE, Operation.WRITE_CODE}),
        writable_paths=("src", "docs"),
    )
    b = _bare(
        permitted_operations=frozenset({Operation.WRITE_CODE, Operation.READ_CODE}),
        writable_paths=("docs", "src"),
    )

    assert a.canonical_payload() == b.canonical_payload()
    assert sign(a, KEY).signature == sign(b, KEY).signature


# ---------------------------------------------------------------------------
# The decision function accepts no model output
# ---------------------------------------------------------------------------
async def test_policy_refuses_anything_that_is_not_a_decision_input() -> None:
    """'The model proposes but never decides', enforced at the signature.

    A ticket understanding, a classification or a plan passed here by mistake must
    fail loudly rather than be interpreted as permission.
    """
    root = create_root()
    fiber = await root.plugin(PolicyServicePlugin, {"default": "allow"})
    try:
        principal = principal_of("alice", "acme")

        model_output = {"category": "bug", "confidence": 0.9, "may_write": True}
        with pytest.raises(TypeError, match="requires an Operation"):
            root.policy.decide(principal, model_output)  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="requires a Principal"):
            root.policy.decide("alice", Operation.READ_CODE)  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="ResourceRef"):
            root.policy.decide(principal, Operation.READ_CODE, {"kind": "repo"})  # type: ignore[arg-type]
    finally:
        await dispose_fiber(fiber)


async def test_policy_can_escalate_to_a_human() -> None:
    root = create_root()
    fiber = await root.plugin(
        PolicyServicePlugin, {"default": "allow", "ask_operations": ["open_pr"]}
    )
    try:
        principal = principal_of("alice", "acme")
        assert root.policy.decide(principal, Operation.READ_CODE) is PolicyDecision.ALLOW
        assert (
            root.policy.decide(principal, Operation.OPEN_PR, ResourceRef("repo", REPO_A.slug))
            is PolicyDecision.ASK
        )
    finally:
        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
async def test_unknown_or_expired_credentials_fail_the_same_way() -> None:
    root = create_root()
    fiber = await root.plugin(PrincipalServicePlugin, {})
    try:
        root.principal.add_token("good", principal_of("alice", "acme"))
        root.principal.add_token(
            "stale",
            principal_of("bob", "acme"),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

        assert root.principal.resolve("good").principal.principal_id == PrincipalId("alice")
        with pytest.raises(AuthenticationFailed, match="unknown credential"):
            root.principal.resolve("nope")
        with pytest.raises(AuthenticationFailed, match="expired"):
            root.principal.resolve("stale")
        with pytest.raises(AuthenticationFailed, match="no credential"):
            root.principal.resolve("")
    finally:
        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# Derivation cannot widen
# ---------------------------------------------------------------------------
def test_derivation_intersects_grant_with_project() -> None:
    """Neither input alone is authority."""
    principal = principal_of("alice", "acme")
    grant = Grant(
        operations=frozenset(Operation),
        repo_prefixes=frozenset({"git.corp/team/*", "git.corp/other/*"}),
        writable_paths=("src", "docs"),
        network=NetworkAccess.ALLOW,
    )
    project = ProjectPolicy(
        repo_prefixes=frozenset({"git.corp/team/*"}),
        allowed_operations=frozenset({Operation.READ_CODE}),
        writable_paths=("src",),
        network=NetworkAccess.DENY,
        budget=Budget(max_tool_calls=5, max_tokens=100, max_wall_seconds=60, max_cost_usd=1),
    )

    constraints = derive(principal, grant, project, key=KEY)

    assert constraints.allowed_repo_prefixes == frozenset({"git.corp/team/*"})
    assert constraints.permitted_operations == frozenset({Operation.READ_CODE})
    assert constraints.writable_paths == ("src",)
    assert constraints.network is NetworkAccess.DENY
    assert constraints.budget.max_tool_calls == 5
    assert constraints.is_signed


def test_a_permissive_policy_does_not_strip_a_specific_grant() -> None:
    """Patterns are not sets, and treating them as sets silently drops everything.

    ``{"git.corp/team/*"} & {"*"}`` is the **empty set** — which would revoke a
    developer's access the moment a permissive policy met their specific grant.
    Enforcing the wrong answer is worse than enforcing none, because it looks like
    it worked.
    """
    grant = Grant(
        operations=frozenset(Operation), repo_prefixes=frozenset({"git.corp/team/*"})
    )
    open_policy = ProjectPolicy.open_project(prefixes=frozenset({"*"}))

    constraints = derive(principal_of("alice", "acme"), grant, open_policy, key=KEY)

    assert constraints.allowed_repo_prefixes == frozenset({"git.corp/team/*"})
    assert constraints.allows_repo(REPO_A)


def test_a_policy_narrows_a_broader_grant() -> None:
    grant = Grant(operations=frozenset(Operation), repo_prefixes=frozenset({"git.corp/*"}))
    policy = ProjectPolicy(repo_prefixes=frozenset({"git.corp/team/*"}))

    constraints = derive(principal_of("alice", "acme"), grant, policy, key=KEY)

    assert constraints.allowed_repo_prefixes == frozenset(), (
        "the grant's broader prefix is not inside the policy's narrower one, so it "
        "does not survive"
    )


def test_a_narrower_grant_survives_a_broader_policy() -> None:
    grant = Grant(
        operations=frozenset(Operation),
        repo_prefixes=frozenset({"git.corp/team/*", "git.corp/other/*"}),
    )
    policy = ProjectPolicy(repo_prefixes=frozenset({"git.corp/team/*"}))

    constraints = derive(principal_of("alice", "acme"), grant, policy, key=KEY)

    assert constraints.allowed_repo_prefixes == frozenset({"git.corp/team/*"})


def test_writable_paths_are_narrowed_by_prefix_not_equality() -> None:
    """A grant of ``src/sub`` is permitted by a policy of ``src``."""
    grant = Grant(operations=frozenset(Operation), writable_paths=("src/sub", "docs"))
    policy = ProjectPolicy(
        allowed_operations=frozenset(Operation), writable_paths=("src",)
    )

    constraints = derive(principal_of("alice", "acme"), grant, policy, key=KEY)

    assert constraints.writable_paths == ("src/sub",)


def test_no_project_policy_is_the_most_restrictive_not_the_most_permissive() -> None:
    """Omission must not read as "no restrictions"."""
    grant = Grant(operations=frozenset(Operation), repo_prefixes=frozenset({"*"}))

    constraints = derive(principal_of("alice", "acme"), grant, None, key=KEY)

    assert constraints.permitted_operations == frozenset()
    assert constraints.allowed_repo_prefixes == frozenset()
    assert constraints.network is NetworkAccess.DENY


# ---------------------------------------------------------------------------
# The scope: authority is not ambient
# ---------------------------------------------------------------------------
async def test_a_plugin_needing_constraints_cannot_load_outside_a_scope() -> None:
    """The fail-closed property, and the reason this milestone exists.

    Not "raises when it reads them" and not "gets None" — the fiber never
    activates. Forgetting to establish a request scope yields a plugin that does
    nothing, rather than one that acts with no limits.
    """
    root = create_root()
    loaded: list[object] = []

    def apply(ctx, cfg):  # noqa: ANN001, ARG001
        loaded.append(ctx.constraints)
        return None

    at_root = await root.plugin({"inject": ["constraints"], "apply": apply})
    assert at_root.get_state().name == "PENDING"
    assert loaded == [], "the plugin body must not have run"

    scope = open_request_scope(root, derive(
        principal_of("alice", "acme"),
        Grant(operations=frozenset(Operation), repo_prefixes=frozenset({"*"})),
        ProjectPolicy.open_project(),
        key=KEY,
    ), key=KEY)

    in_scope = await scope.ctx.plugin({"inject": ["constraints"], "apply": apply})

    assert in_scope.get_state().name == "ACTIVE"
    assert len(loaded) == 1


async def test_request_scopes_do_not_see_each_other() -> None:
    root = create_root()
    grant = Grant(operations=frozenset(Operation), repo_prefixes=frozenset({"*"}))

    first = open_request_scope(
        root,
        derive(principal_of("alice", "acme"), grant, ProjectPolicy.open_project(), key=KEY),
        key=KEY,
    )
    second_scope = open_request_scope(root, _bare(), key=KEY, verify_signature=False)

    assert first.constraints.principal_id == PrincipalId("alice")
    assert second_scope.constraints.principal_id == PrincipalId("alice")
    assert first.ctx.interface("constraints") != second_scope.ctx.interface("constraints"), (
        "each scope must have its own interface label, or they are not isolated"
    )


async def test_a_forged_constraint_set_never_gets_a_scope() -> None:
    from dataclasses import replace

    root = create_root()
    forged = replace(sign(_bare(), KEY), permitted_operations=frozenset(Operation))

    with pytest.raises(SignatureInvalid):
        open_request_scope(root, forged, key=KEY)


async def test_an_unsigned_set_cannot_open_a_scope() -> None:
    root = create_root()
    with pytest.raises(SignatureInvalid, match="unsigned"):
        open_request_scope(root, _bare(), key=KEY)


async def test_verification_can_be_switched_off_explicitly() -> None:
    """Off is allowed, but it is a visible argument rather than a silent default."""
    root = create_root()
    scope = open_request_scope(root, _bare(), key=KEY, verify_signature=False)
    assert scope.constraints.permitted_operations == frozenset({Operation.READ_CODE})


# ---------------------------------------------------------------------------
# End to end through the boundary
# ---------------------------------------------------------------------------
async def test_authenticate_produces_a_scoped_context() -> None:
    root = create_root()
    fiber = await root.plugin(PrincipalServicePlugin, {})
    try:
        root.principal.add_token(
            "tok",
            principal_of("alice", "acme", teams=("team-a",)),
            Grant(
                operations=frozenset({Operation.READ_CODE, Operation.WRITE_CODE}),
                repo_prefixes=frozenset({"git.corp/team/*"}),
                writable_paths=("src",),
            ),
        )

        context = authenticate(root, "tok", project=ProjectPolicy.open_project(), key=KEY)

        assert context.principal.principal_id == PrincipalId("alice")
        assert context.constraints.is_signed
        assert context.constraints.allows_repo(REPO_A)
        assert not context.constraints.allows_repo(REPO_B)
        assert context.ctx.interface("constraints") != root.interface("constraints")
    finally:
        await dispose_fiber(fiber)


async def test_authenticate_refuses_an_unknown_credential_before_scoping() -> None:
    root = create_root()
    fiber = await root.plugin(PrincipalServicePlugin, {})
    try:
        with pytest.raises(AuthenticationFailed):
            authenticate(root, "nope", project=ProjectPolicy.open_project(), key=KEY)
    finally:
        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# The permission story, exercised against real seams
# ---------------------------------------------------------------------------
async def test_a_caller_limited_to_one_repo_cannot_read_another() -> None:
    """Derived constraints, carried into the seam that would leak."""
    root = create_root()
    retriever_fiber = await root.plugin(
        resolve_plugin("forgepilot.providers.retrieve.mock:MockRetrieverPlugin"), {}
    )
    try:
        retriever = root.retriever
        retriever.add_file(REPO_A, "src/ok.py", "VISIBLE = 1\n")
        retriever.add_file(REPO_B, "src/secret.py", "SECRET = 'leak'\n")

        constraints = derive(
            principal_of("alice", "acme"),
            Grant(
                operations=frozenset(Operation),
                repo_prefixes=frozenset({"git.corp/team/*"}),
            ),
            ProjectPolicy.open_project(),
            key=KEY,
        )

        hits = await retriever.search(
            SearchQuery(pattern="SECRET|VISIBLE", repos=(REPO_A, REPO_B), ref="main"),
            constraints,
        )

        assert [hit.location.repo for hit in hits] == [REPO_A]
        assert retriever.denied_repo_requests == 1, "the seam refused it itself"
    finally:
        await dispose_fiber(retriever_fiber)


async def test_a_resumed_session_inherits_narrowed_not_original_authority() -> None:
    """Permissions shrink between sessions; the resumed one must shrink with them.

    Otherwise resuming would be a way to recover authority you have since lost.
    """
    principal = principal_of("alice", "acme")
    grant = Grant(operations=frozenset(Operation), repo_prefixes=frozenset({"*"}))

    original = derive(principal, grant, ProjectPolicy.open_project(), key=KEY)
    assert original.allows_repo(REPO_B)

    # The same person, after a team change.
    narrowed_grant = Grant(
        operations=frozenset({Operation.READ_CODE}),
        repo_prefixes=frozenset({"git.corp/team/*"}),
    )
    resumed = derive(principal, narrowed_grant, ProjectPolicy.open_project(), key=KEY)

    assert not resumed.allows_repo(REPO_B)
    assert resumed.permitted_operations == frozenset({Operation.READ_CODE})
    assert resumed.is_signed, "re-derived authority is freshly signed, not inherited"


# ---------------------------------------------------------------------------
# Credentials from the environment
#
# Without this the token table is only ever filled by tests, and the CLI cannot
# authenticate at all. Tokens come from the environment rather than config for
# the same reason API keys do: a config file is committed and dumped.
# ---------------------------------------------------------------------------
async def test_credentials_load_from_the_environment(monkeypatch) -> None:
    import json as _json

    monkeypatch.setenv(
        "TEST_TOKENS",
        _json.dumps([
            {
                "token": "dev-token",
                "principal_id": "alice",
                "tenant_id": "acme",
                "teams": ["team-a"],
                "operations": ["read_code", "write_code"],
                "repo_prefixes": ["git.corp/team/*"],
                "writable_paths": ["src"],
            }
        ]),
    )
    root = create_root()
    fiber = await root.plugin(PrincipalServicePlugin, {"tokens_env": "TEST_TOKENS"})
    try:
        record = root.principal.resolve("dev-token")

        assert record.principal.principal_id == PrincipalId("alice")
        assert record.principal.team_ids == frozenset({"team-a"})
        assert record.grant.operations == frozenset(
            {Operation.READ_CODE, Operation.WRITE_CODE}
        )
        assert record.grant.repo_prefixes == frozenset({"git.corp/team/*"})
        assert record.grant.writable_paths == ("src",)
        # Network defaults to deny when the entry does not say otherwise.
        assert record.grant.network is NetworkAccess.DENY
    finally:
        await dispose_fiber(fiber)


async def test_a_malformed_credential_table_fails_loudly(monkeypatch) -> None:
    """A silently-empty table is much harder to diagnose than a parse error."""
    monkeypatch.setenv("TEST_TOKENS", "{not json")
    root = create_root()

    with pytest.raises(AuthenticationFailed, match="not valid JSON"):
        await root.plugin(PrincipalServicePlugin, {"tokens_env": "TEST_TOKENS"})


async def test_an_entry_without_a_token_is_refused(monkeypatch) -> None:
    import json as _json

    monkeypatch.setenv("TEST_TOKENS", _json.dumps([{"principal_id": "alice"}]))
    root = create_root()

    with pytest.raises(AuthenticationFailed, match="no 'token'"):
        await root.plugin(PrincipalServicePlugin, {"tokens_env": "TEST_TOKENS"})


async def test_no_token_environment_variable_is_not_an_error() -> None:
    """A deployment that authenticates another way must still boot."""
    root = create_root()
    fiber = await root.plugin(PrincipalServicePlugin, {"tokens_env": "DEFINITELY_UNSET_VAR"})
    try:
        assert root.principal.reload_from_env() == 0
    finally:
        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# Narrowing writable paths is an intersection, not a filter
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("grant", "policy", "want"),
    [
        # Both of these returned an EMPTY set, from opposite sides. The old rule
        # kept a grant path only when it already fell inside a policy path, so a
        # grant wider than the policy matched nothing and a grant narrower than a
        # permissive policy matched nothing either.
        ((".",), ("src",), ("src",)),
        (("src",), (".",), ("src",)),
        # The one case a filter and an intersection agree on, and the only shape
        # any existing test used — which is why nobody noticed.
        (("src",), ("src",), ("src",)),
        (("src", "tests"), ("src",), ("src",)),
        (("src",), ("tests",), ()),
        ((".",), (".",), (".",)),
        (("src/a",), ("src",), ("src/a",)),
        (("src",), ("src/a",), ("src/a",)),
    ],
)
def test_writable_paths_narrow_by_intersection(grant, policy, want) -> None:
    """A project restricted to `src/` behind a principal granted everything.

    The refusal this produced said *"nothing is writable"* — a true statement
    about the intersection the code computed and a false one about the authority
    the caller held. Found by an eval task, not by a test, because every test
    happened to use the same set on both sides.
    """
    from forgepilot.identity.constraints import _narrowed_paths

    assert _narrowed_paths(grant, policy) == want


def test_a_permissive_grant_on_a_restricted_project_can_still_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property, asserted through the thing that enforces it.

    `_narrowed_paths` returning the right tuple is only useful if
    `allows_write_to` then agrees — and it is the *guard* that refused, so the
    end-to-end statement is the one worth pinning.
    """
    from forgepilot.seams.security import ExecutionConstraints, Operation

    # `derive` signs what it returns, and an unsigned set is refused rather
    # than treated as "unverified but probably fine".
    monkeypatch.setenv("FORGEPILOT_SIGNING_KEY", "test-key")

    principal = Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme"))
    constraints = derive(
        principal,
        Grant(
            repos=frozenset({REPO_A}),
            operations=frozenset(Operation),
            # The grant says "the whole checkout"...
            writable_paths=(".",),
        ),
        # ...and the project says "only src".
        ProjectPolicy(repos=frozenset({REPO_A}), writable_paths=("src",)),
        key=None,
    )

    assert constraints.allows_write_to("src/metrics.py"), (
        "a grant of the whole tree was narrowed to nothing by a restricted project"
    )
    assert not constraints.allows_write_to("settings.py"), (
        "the project's restriction was not applied"
    )
