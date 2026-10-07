"""HMAC signing for :class:`ExecutionConstraints`.

Why this exists: constraints are the single object carrying authority from the
request boundary down into tools, and constraints are **passed as ordinary
arguments**. Anything that can construct one can hand a tool authority it was
never granted. A signature makes that a detectable act rather than an invisible
one.

What it does and does not buy, stated plainly:

- It **does** stop a plugin, a tool, or a prompt-injected code path from minting
  a wider set out of nothing, and it **does** detect a constraint object altered
  after the fact.
- It **does not** stop code that holds the key, and in-process it cannot stop
  code from calling the tool with no constraints at all — the defence against
  that is the seam re-checking (`allows_repo`, guards) and the request scope.
  Signing narrows the forgery surface; it is not the whole boundary.

The key is read from the environment rather than a config file so that it does
not end up in `--dump-config`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import replace

from forgepilot.identity.errors import SignatureInvalid, SigningKeyMissing
from forgepilot.seams.security import ExecutionConstraints

__all__ = ["ENV_KEY", "load_key", "sign", "verify", "signed"]

#: Where the signing key is expected. Deliberately an environment variable:
#: config files are dumped by `--dump-config`, and a key in a dump is a leaked key.
ENV_KEY = "FORGEPILOT_SIGNING_KEY"


def load_key() -> bytes:
    """Read the signing key, failing closed when it is absent."""
    raw = os.environ.get(ENV_KEY)
    if not raw:
        raise SigningKeyMissing(
            f"{ENV_KEY} is not set. Constraints cannot be signed or verified, and "
            f"running with unverifiable authority is worse than not running."
        )
    return raw.encode("utf-8")


def _digest(constraints: ExecutionConstraints, key: bytes) -> str:
    payload = json.dumps(
        constraints.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def sign(constraints: ExecutionConstraints, key: bytes | None = None) -> ExecutionConstraints:
    """Return a copy of ``constraints`` carrying a fresh signature."""
    material = load_key() if key is None else key
    return replace(constraints, signature=_digest(constraints, material))


def verify(constraints: ExecutionConstraints, key: bytes | None = None) -> None:
    """Raise :class:`SignatureInvalid` unless the signature matches.

    An unsigned set is invalid, not "unverified but probably fine" — otherwise
    omitting the signature would be the easy way past the check.
    """
    material = load_key() if key is None else key

    if not constraints.is_signed:
        raise SignatureInvalid(
            "constraints are unsigned. An unsigned set is refused rather than trusted, "
            "or omitting the signature would be the easiest way past this check."
        )

    expected = _digest(constraints, material)
    if not hmac.compare_digest(expected, constraints.signature):
        raise SignatureInvalid(
            "constraints do not match their signature: they were altered after signing, "
            "or signed with a different key."
        )


def signed(constraints: ExecutionConstraints, key: bytes | None = None) -> ExecutionConstraints:
    """Alias for :func:`sign`, for readability at call sites."""
    return sign(constraints, key)
