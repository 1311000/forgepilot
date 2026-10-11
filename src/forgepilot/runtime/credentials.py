"""The last mile of the credential channel: substitution, and the literal scan.

The seam says what a name *is*; this module decides where the value may appear,
and it is deliberately the only place that knows both halves. Two functions, two
opposite directions:

- :func:`resolve_placeholders` puts the value **in**, immediately before a process
  is handed the command.
- :func:`scrub_values` takes it back **out**, for anything that will be recorded —
  a diagnostic ``argv``, an error message, a log line.

Scrubbing is not the same job as redaction and does not replace it. Redaction
(pattern-driven, at the log) is the net; scrubbing is exact, cheap, and knows the
value, so it catches what no pattern would — a secret that does not *look* like
one. The two are layered on purpose: either alone leaves a gap the other covers.
"""

from __future__ import annotations

from collections.abc import Mapping

from forgepilot.runtime.redaction import RedactionMark, Redactor, default_redactor
from forgepilot.seams.credentials import (
    CredentialChannel,
    UnknownCredential,
    placeholder,
    placeholders,
)

__all__ = ["literal_secret", "resolve_placeholders", "scrub_values"]


def literal_secret(text: str, redactor: Redactor | None = None) -> RedactionMark | None:
    """The first secret-shaped literal in ``text``, if there is one.

    Rules 1–6 only: the high-entropy rule is excluded **even when a deployment
    has switched it on**. A refusal has to be defensible to the model that
    receives it, and "this string looks random to me" is not — it would refuse a
    base64 build artefact or a content hash, and a refusal a model cannot make
    sense of is one it routes around.
    """
    _, report = (redactor or default_redactor()).redact_text(text or "")
    for mark in report.marks:
        if mark.kind != "high-entropy":
            return mark
    return None


async def resolve_placeholders(
    text: str, channel: CredentialChannel | None
) -> tuple[str, dict[str, str]]:
    """Replace every ``${NAME}`` with its value, returning what was substituted.

    The second return value **is the secret**. It exists so the caller can scrub
    the value back out of anything it is about to record — the substitution is
    allowed to reach the process, and nothing else.

    Raises :class:`UnknownCredential` for a name nobody holds, including when
    there is no channel at all. Refusing here means the failure is reported as
    "that name has no secret", which the model can act on, instead of as a shell
    error several steps later that looks like a problem with the command.
    """
    names = placeholders(text)
    if not names:
        return text, {}
    if channel is None:
        raise UnknownCredential(
            "this command uses ${...} but this session has no credential channel, so "
            "there is nothing to substitute. The names have to be declared by whoever "
            "runs the deployment; without one, run the command without the secret."
        )
    values: dict[str, str] = {}
    for name in names:
        values[name] = await channel.resolve(name)
    resolved = text
    for name, value in values.items():
        resolved = resolved.replace(placeholder(name), value)
    return resolved, values


def scrub_values(text: str, values: Mapping[str, str]) -> str:
    """Put the placeholders back, for anything that will be recorded."""
    if not text or not values:
        return text
    scrubbed = text
    for name, value in values.items():
        if value:
            scrubbed = scrubbed.replace(value, placeholder(name))
    return scrubbed
