"""Identity-layer errors."""

from __future__ import annotations

from forgepilot.errors import ForgePilotError

__all__ = [
    "IdentityError",
    "AuthenticationFailed",
    "SignatureInvalid",
    "SigningKeyMissing",
    "PolicyViolation",
]


class IdentityError(ForgePilotError):
    """Base class for identity, authorisation and signing failures."""


class AuthenticationFailed(IdentityError):
    """A caller could not be identified.

    Raised for an unknown token, an expired one, or a malformed one. All three
    are the same answer to the caller: no identity, so no request.
    """


class SignatureInvalid(IdentityError):
    """Constraints did not match their signature.

    Either they were altered in flight, or they were never signed by a holder of
    the key. Both mean the same thing here: refuse.
    """


class SigningKeyMissing(IdentityError):
    """No signing key is configured.

    Fail-closed rather than "skip verification": a deployment that forgot to
    configure a key should not silently run with unverifiable authority.
    """


class PolicyViolation(IdentityError):
    """A policy decision refused an operation."""
