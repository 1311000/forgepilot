"""P17: a signature that nobody verifies is a field, not a check.

`assemble_session` — the only production path to a request scope — used to pass
`verify_signature=False` unconditionally. `deps.authenticate` could verify, but nothing
in `src/` called it, so "the constraints are HMAC-signed" was true of the object and false
of the system. These tests pin the four sentences that replaced it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from forgepilot.identity.constraints import Grant, ProjectPolicy, derive
from forgepilot.identity.errors import SignatureInvalid
from forgepilot.identity.principal import Principal, PrincipalId, TenantId
from forgepilot.identity.signing import ENV_KEY, configured_key
from forgepilot.runtime.assembly import assemble_session
from forgepilot.seams.security import Operation

KEY = b"a signing key that exists only in this test"


def _principal() -> Principal:
    return Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme"))


def _signed():
    policy = ProjectPolicy.open_project(prefixes=frozenset({"git.corp/team/*"}))
    grant = Grant(
        operations=frozenset({Operation.READ_CODE}),
        repo_prefixes=frozenset({"git.corp/team/*"}),
    )
    return derive(_principal(), grant, policy, key=KEY)


@pytest.fixture
async def wired(rig):
    """The rig with the one service `assemble_session` refuses to run without."""
    from forgepilot.config import resolve_plugin

    await rig.root.plugin(
        resolve_plugin("forgepilot.identity.policy:PolicyServicePlugin"), {"default": "allow"}
    )
    return rig


def test_configured_key_reads_the_named_variable(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "configured")
    assert configured_key() == b"configured"
    monkeypatch.delenv(ENV_KEY)
    assert configured_key() is None
    # Whitespace is not a key: signing with b"" would look like a configured deployment.
    monkeypatch.setenv(ENV_KEY, "   ")
    assert configured_key() is None


async def test_a_signed_authority_without_a_key_is_refused(wired, monkeypatch):
    """The refusal is the point: an unchecked signature is worse than none."""
    constraints = _signed()
    assert constraints.is_signed
    monkeypatch.delenv(ENV_KEY, raising=False)

    with pytest.raises(ValueError, match="[Uu]nchecked signature"):
        await assemble_session(wired.root, constraints, principal=_principal())


async def test_a_signed_authority_is_accepted_with_the_configured_key(wired, monkeypatch):
    constraints = _signed()
    monkeypatch.setenv(ENV_KEY, KEY.decode())

    runtime = await assemble_session(wired.root, constraints, principal=_principal())
    assert runtime.session is not None


async def test_a_tampered_authority_is_refused_though_the_key_is_there(wired, monkeypatch):
    """Verification, not just presence: the signature has to match the payload."""
    constraints = _signed()
    tampered = replace(constraints, allowed_repo_prefixes=frozenset({"git.corp/evil/*"}))
    monkeypatch.setenv(ENV_KEY, KEY.decode())

    with pytest.raises(SignatureInvalid):
        await assemble_session(wired.root, tampered, principal=_principal())


async def test_an_unsigned_authority_is_accepted_when_no_key_is_configured(
    wired, constraints_factory
):
    """A deployment with no key has no signed authority to check, and says so by being empty."""
    constraints = constraints_factory(Operation.READ_CODE)
    assert not constraints.is_signed

    runtime = await assemble_session(
        wired.root,
        constraints,
        principal=Principal(
            principal_id=PrincipalId(constraints.principal_id),
            tenant_id=TenantId(constraints.tenant_id),
        ),
    )
    assert runtime.session is not None
