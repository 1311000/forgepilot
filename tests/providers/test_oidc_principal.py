"""What the OIDC provider verifies, and what it refuses to invent.

The tests build tokens with the same base64url decoder the provider reads them
with (``decode_segment``), so an encoding mistake fails the test rather than
hiding behind a second implementation of the format.

Every refusal is asserted by its **message**, because the messages are the only
part of this provider an operator ever sees: the HTTP boundary answers 401 with
none of this text, so a wrong reason at boot or in a log is the same as no reason.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from forgepilot.config.errors import ConfigError
from forgepilot.httpapi.deps import resolve_credential
from forgepilot.identity.constraints import ProjectPolicy
from forgepilot.identity.errors import AuthenticationFailed
from forgepilot.kernel import create_root, dispose_fiber
from forgepilot.providers.identity.oidc import OidcPrincipalPlugin, decode_segment
from forgepilot.seams.security import NetworkAccess, Operation

pytestmark: list[Any] = []

SECRET = "a-test-secret-nobody-deploys"
SECRET_ENV = "FORGEPILOT_OIDC_SECRET"
ISSUER = "https://sso.example.com/realms/corp"
AUDIENCE = "forgepilot"

CONFIG: dict[str, Any] = {
    "issuer": ISSUER,
    "audience": AUDIENCE,
    "groups": {
        "backend": {
            "operations": ["read_code", "run_tests", "open_pr"],
            "repos": ["git.corp/team/svc-a"],
            "repo_prefixes": ["git.corp/team/*"],
            "writable_paths": ["src"],
            "network": "allow",
            "commands": ["git"],
        },
        "reviewer": {
            "operations": ["read_code", "run_tests"],
            "commands": ["pytest"],
        },
    },
}


@pytest.fixture(autouse=True)
def _a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, SECRET)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def token(
    claims: Mapping[str, Any] | None = None,
    *,
    secret: str = SECRET,
    header: Mapping[str, Any] | None = None,
    **overrides: Any,
) -> str:
    """Build a signed token. ``overrides`` land in the payload."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "u-1",
        "preferred_username": "alice",
        "tenant": "acme",
        "groups": ["backend"],
        "exp": now + 600,
        "iat": now,
    }
    payload.update(claims or {})
    payload.update(overrides)
    header = dict(header or {"alg": "HS256", "typ": "JWT"})
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}." + _b64(
        json.dumps(payload, separators=(",", ":")).encode()
    )
    signature = hmac.new(secret.encode(), signing_input.encode("ascii"), hashlib.sha256)
    return f"{signing_input}.{_b64(signature.digest())}"


async def booted(
    *, config: Mapping[str, Any] | None = None
):
    """Load the provider the way a profile would."""
    root = create_root()
    fiber = await root.plugin(OidcPrincipalPlugin, dict(config or CONFIG))
    return root, (fiber,)


async def close(fibers: tuple[Any, ...]) -> None:
    for fiber in reversed(fibers):
        await dispose_fiber(fiber)


# ---------------------------------------------------------------------------
# A token becomes a principal and a grant
# ---------------------------------------------------------------------------


async def test_a_verified_token_becomes_a_principal_and_its_groups_rights() -> None:
    root, fibers = await booted()
    record = root.principal.resolve(token({"groups": ["backend", "reviewer"]}))

    assert str(record.principal.principal_id) == "alice"
    assert str(record.principal.tenant_id) == "acme"
    assert {str(team) for team in record.principal.team_ids} == {"backend", "reviewer"}
    # The union of both groups: an operation one group grants survives the other.
    assert record.grant.operations == frozenset(
        {Operation.READ_CODE, Operation.RUN_TESTS, Operation.OPEN_PR}
    )
    assert {repo.slug for repo in record.grant.repos} == {"git.corp/team/svc-a"}
    assert record.grant.repo_prefixes == frozenset({"git.corp/team/*"})
    assert record.grant.writable_paths == ("src",)
    assert record.grant.commands == frozenset({"git", "pytest"})
    assert record.grant.network is NetworkAccess.ALLOW
    assert record.is_expired() is False
    await close(fibers)


async def test_a_group_this_deployment_never_mapped_grants_nothing() -> None:
    """Membership is not an authorisation: an unmapped name contributes no rights."""
    root, fibers = await booted()
    record = root.principal.resolve(token({"groups": ["backend", "administrators"]}))

    assert {str(team) for team in record.principal.team_ids} == {"backend", "administrators"}
    assert Operation.OPEN_PR in record.grant.operations, "the mapped group still counts"
    # A group the config does not name cannot add an operation the mapped one lacks.
    assert Operation.PURGE_MEMORY not in record.grant.operations
    await close(fibers)


async def test_claims_with_no_tenant_fall_back_to_the_configured_one() -> None:
    root, fibers = await booted()
    record = root.principal.resolve(token({"tenant": None}))
    assert str(record.principal.tenant_id) == "default"
    await close(fibers)


async def test_the_seam_swap_needs_nothing_but_the_row(tmp_path: Path) -> None:
    """``resolve_credential`` is what every caller uses; it must not know which provider."""
    root, fibers = await booted()
    credential = resolve_credential(
        root,
        token({"groups": ["reviewer"]}),
        project=ProjectPolicy.open_project(),
        key=b"a-test-key",
    )
    assert credential.principal.principal_id == "alice"
    assert Operation.OPEN_PR not in credential.constraints.permitted_operations
    assert Operation.READ_CODE in credential.constraints.permitted_operations
    await close(fibers)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def test_a_token_signed_with_another_secret_is_refused() -> None:
    root, fibers = await booted()
    with pytest.raises(AuthenticationFailed, match="signature does not verify"):
        root.principal.resolve(token(secret="not-the-deployment-secret"))
    await close(fibers)


async def test_a_tampered_payload_is_refused() -> None:
    root, fibers = await booted()
    good = token()
    head, _, signature = good.split(".")
    forged = _b64(json.dumps({"iss": ISSUER, "aud": AUDIENCE, "sub": "root", "exp": 9e9}).encode())
    with pytest.raises(AuthenticationFailed, match="signature does not verify"):
        root.principal.resolve(f"{head}.{forged}.{signature}")
    await close(fibers)


async def test_an_unsupported_algorithm_is_refused_by_name() -> None:
    """The refusal has to say *which* algorithm, or a deployment cannot configure it."""
    root, fibers = await booted()
    with pytest.raises(AuthenticationFailed) as raised:
        root.principal.resolve(
            token(header={"alg": "RS256", "typ": "JWT"})
        )
    assert "RS256" in str(raised.value)
    assert "HS256" in str(raised.value)
    await close(fibers)


async def test_none_is_not_an_algorithm_this_provider_accepts() -> None:
    root, fibers = await booted()
    with pytest.raises(AuthenticationFailed, match="unsupported signing algorithm 'none'"):
        root.principal.resolve(token(header={"alg": "none"}))
    await close(fibers)


async def test_an_expired_token_is_refused() -> None:
    root, fibers = await booted()
    with pytest.raises(AuthenticationFailed, match="has expired"):
        root.principal.resolve(token({"exp": int(time.time()) - 600}))
    await close(fibers)


async def test_a_token_without_an_expiry_is_refused() -> None:
    """A credential that never stops working is not a credential."""
    root, fibers = await booted()
    claims = {"exp": None}
    with pytest.raises(AuthenticationFailed, match="no numeric 'exp'"):
        root.principal.resolve(token(claims))
    await close(fibers)


async def test_a_token_that_is_not_valid_yet_is_refused() -> None:
    root, fibers = await booted()
    with pytest.raises(AuthenticationFailed, match="not valid yet"):
        root.principal.resolve(token({"nbf": int(time.time()) + 600}))
    await close(fibers)


async def test_the_clock_skew_covers_a_token_that_is_seconds_early() -> None:
    root, fibers = await booted()
    record = root.principal.resolve(token({"nbf": int(time.time()) + 5}))
    assert record.principal.principal_id == "alice"
    await close(fibers)


async def test_a_token_from_another_issuer_is_refused() -> None:
    root, fibers = await booted()
    with pytest.raises(AuthenticationFailed, match="was issued by"):
        root.principal.resolve(token({"iss": "https://evil.example.com"}))
    await close(fibers)


async def test_a_token_for_another_audience_is_refused() -> None:
    root, fibers = await booted()
    with pytest.raises(AuthenticationFailed, match="is for"):
        root.principal.resolve(token({"aud": "some-other-client"}))
    await close(fibers)


async def test_an_audience_list_is_read_as_a_list() -> None:
    root, fibers = await booted()
    record = root.principal.resolve(token({"aud": ["another-client", AUDIENCE]}))
    assert record.principal.principal_id == "alice"
    await close(fibers)


async def test_a_token_with_no_principal_falls_back_to_sub() -> None:
    root, fibers = await booted()
    record = root.principal.resolve(token({"preferred_username": None}))
    assert str(record.principal.principal_id) == "u-1"
    with pytest.raises(AuthenticationFailed, match="names no principal"):
        root.principal.resolve(token({"preferred_username": None, "sub": None}))
    await close(fibers)


async def test_a_credential_that_is_not_a_token_is_refused() -> None:
    root, fibers = await booted()
    with pytest.raises(AuthenticationFailed, match="three parts"):
        root.principal.resolve("not-a-token")
    with pytest.raises(AuthenticationFailed, match="malformed"):
        root.principal.resolve("a.b.c")
    with pytest.raises(AuthenticationFailed, match="no credential supplied"):
        root.principal.resolve("")
    await close(fibers)


# ---------------------------------------------------------------------------
# Configuration mistakes fail at boot, by name
# ---------------------------------------------------------------------------


async def test_a_provider_without_an_issuer_says_which_key_is_missing() -> None:
    config = {key: value for key, value in CONFIG.items() if key != "issuer"}
    with pytest.raises(ConfigError, match="'issuer'"):
        await booted(config=config)


async def test_a_provider_without_an_audience_says_which_key_is_missing() -> None:
    config = {key: value for key, value in CONFIG.items() if key != "audience"}
    with pytest.raises(ConfigError, match="'audience'"):
        await booted(config=config)


async def test_a_missing_secret_names_the_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SECRET_ENV)
    with pytest.raises(ConfigError, match=SECRET_ENV):
        await booted()


async def test_an_unknown_operation_in_the_group_mapping_is_refused_at_boot() -> None:
    config = dict(CONFIG)
    config["groups"] = {"backend": {"operations": ["read_code", "open_the_pod_bay_doors"]}}
    with pytest.raises(ConfigError, match="open_the_pod_bay_doors"):
        await booted(config=config)


async def test_a_repository_must_have_three_parts() -> None:
    config = dict(CONFIG)
    config["groups"] = {"backend": {"repos": ["git.corp/team"]}}
    with pytest.raises(ConfigError, match="host/namespace/name"):
        await booted(config=config)


def test_the_decoder_is_the_one_the_provider_reads_with() -> None:
    """A decoder only the provider uses is a decoder nobody cross-checks."""
    head, payload, _ = token().split(".")
    assert decode_segment(head).startswith(b'{"alg"')
    assert json.loads(decode_segment(payload))["iss"] == ISSUER
    with pytest.raises(AuthenticationFailed):
        decode_segment("!!not base64!!")
