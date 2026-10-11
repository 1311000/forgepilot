"""Identity, authorisation and the propagation of authority.

The path a request takes through here::

    credential -> Principal + Grant        (principal.py — "who is this?")
        -> ExecutionConstraints            (constraints.py — "what may they do here?")
        -> signed                          (signing.py — "can this be forged?")
        -> a request scope                 (scope.py — "where does it apply?")

Each step answers one question, and no step can widen what the previous one
established. Two of the rules this package exists to hold:

- **The decision function takes no model output.** :meth:`PolicyServicePlugin.decide`
  accepts a ``Principal``, an ``Operation`` and an optional ``ResourceRef``, and
  refuses anything else at runtime. "The local model proposes but never decides"
  is a property of the signature, not a convention.
- **Authority is scoped, not ambient.** A plugin declaring
  ``inject = ["constraints"]`` cannot activate outside a request scope, so
  forgetting to establish one yields a plugin that does nothing rather than one
  that acts unbounded.
"""

from forgepilot.identity.constraints import ProjectPolicy, derive
from forgepilot.identity.errors import (
    AuthenticationFailed,
    IdentityError,
    PolicyViolation,
    SignatureInvalid,
    SigningKeyMissing,
)
from forgepilot.identity.policy import (
    PolicyDecision,
    PolicyServicePlugin,
    ResourceRef,
)
from forgepilot.identity.principal import Grant, PrincipalServicePlugin, TokenRecord
from forgepilot.identity.scope import RequestScope, open_request_scope
from forgepilot.identity.signing import ENV_KEY, load_key, sign, verify

__all__ = [
    # principal
    "Grant",
    "TokenRecord",
    "PrincipalServicePlugin",
    # policy
    "PolicyDecision",
    "ResourceRef",
    "PolicyServicePlugin",
    # constraints
    "ProjectPolicy",
    "derive",
    # signing
    "sign",
    "verify",
    "load_key",
    "ENV_KEY",
    # scope
    "RequestScope",
    "open_request_scope",
    # errors
    "IdentityError",
    "AuthenticationFailed",
    "SignatureInvalid",
    "SigningKeyMissing",
    "PolicyViolation",
]
