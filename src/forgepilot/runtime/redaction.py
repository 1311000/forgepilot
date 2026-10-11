"""Redaction: the gate every event passes *before* it is constructed.

**What this is.** A pattern-driven filter that recognises sensitive material —
credentials, tokens, private keys, internal URLs — and replaces it with a
*typed* placeholder. It runs on the write path, inside
:meth:`forgepilot.runtime.session.Session.append`, which is the single door into
the log. Because the model only ever sees what the log holds
(``runtime/log_guard.py``: model-visible means logged), that one gate protects
the conversation and the storage at the same time. There is no path where the
log stays clean and the model still saw the secret.

**Why the write path and not the storage layer.** Payloads above
``DEFAULT_INLINE_LIMIT`` spill to a blob file
(``providers/session_store/jsonl.py:73``, ``:413``): the blob is written from the
event's ``data`` and the event keeps only a descriptor. Redacting at that layer
would miss every large tool result — which is exactly where a stray credential
is most likely to appear. Redacting in ``append()`` catches the payload while it
is still a dictionary.

**Why a placeholder keeps a name.** ``providers/vcs/github.py:96`` learned this
the hard way: replacing too much "would hide the diagnosis along with the
secret". ``«redacted:token»`` says what was removed without saying what it was.
The fingerprint beside it is a *salted* HMAC, so a later reader can tell that the
same value appeared twice without being able to recover or confirm it.

**Failure is degradation, not a crash.** The doctrine this implements is
explicit — a failing detector must not stop the agent, and the raw value must
not land either. So a rule that raises replaces the whole text with a metadata
stub and the caller records ``redaction/failed``. Nothing raw is written and the
run continues; that is the "fail-closed" of this layer (closed for the *data*,
not for the *task*).

**No IO, no clock, no model.** The write path cannot afford a failure mode with
a cost, so this module is a pure function of its inputs. The one piece of
process state is the salt.

**There is no kill switch.** A gate that can be turned off in a config file is
off in practice, so the only switch here belongs to one *rule* (the high-entropy
heuristic, which is noisy by nature) and defaults to off. Removing the gate
altogether takes a code edit, which is what it should take.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from forgepilot.seams.security import Sensitivity

__all__ = [
    "FAILED_PREFIX",
    "HIGH_ENTROPY_ENV",
    "INTERNAL_HOSTS_ENV",
    "PLACEHOLDER_PREFIX",
    "SECRET_EVENT_KIND",
    "RedactionMark",
    "RedactionReport",
    "Redactor",
    "Rule",
    "collapse_payload",
    "default_redactor",
    "reset_default_redactor",
]

#: A redacted value always looks like this. Anything starting with it is already
#: redacted, which is what makes a second pass a no-op.
PLACEHOLDER_PREFIX = "«redacted:"

#: The degraded form: the detector failed, so the whole text became metadata.
#: Deliberately a *different* prefix, so a reader can tell "we know this is a
#: token" from "we could not tell, so we kept nothing".
FAILED_PREFIX = "«redaction-failed:"

#: What a payload collapses to when a *SECRET*-classified rule fired. See
#: :func:`collapse_payload` for why the whole payload goes, not just the match.
SECRET_EVENT_KIND = "secret-event"

#: Token families recognisable by their own prefix. A prefix list rather than
#: "any long string" on purpose: a rule that fires on ordinary identifiers gets
#: switched off within a week, and a detector that cries wolf protects nothing.
_TOKEN_PREFIXES = (
    "sk-",
    "sk_",
    "sk_live_",
    "rk_live_",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "glpat-",
    "xoxb-",
    "xoxp-",
    "xoxa-",
    "xoxr-",
    "AKIA",
    "ASIA",
    "AIza",
    "ya29.",
    "hf_",
    "r8_",
    "npm_",
    "pypi-",
    "dop_v1_",
    "shpat_",
    "sq0atp-",
)

_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

_TOKEN = re.compile(
    r"(?:" + "|".join(re.escape(prefix) for prefix in _TOKEN_PREFIXES) + r")[A-Za-z0-9_.\-]{8,}"
)

_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}")

#: ``KEY = value`` / ``"key": "value"`` — the shape a credential takes in a
#: ``.env`` file, a shell command or a tool argument. The key name is kept so
#: the reader still sees *which* credential was removed.
#:
#: The lookahead after the name is load-bearing: without it ``max_tokens=200000``
#: matches (it contains "token") and a budget number becomes a placeholder, which
#: corrupts the log's fidelity in exchange for nothing. A credential *name* ends
#: where the next alphanumeric does not start.
_ASSIGNMENT = re.compile(
    r"\b(?P<key>[A-Za-z0-9_]*"
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|CLIENT_?SECRET|"
    r"AUTHORIZATION|CREDENTIAL|PASSPHRASE)"
    r"(?![A-Za-z0-9])[A-Za-z0-9_]*)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<quote>['\"]?)"
    r"(?P<value>[^\s'\";,]{4,})"
    r"(?P=quote)",
    re.IGNORECASE,
)

_AUTH_HEADER = re.compile(
    r"\b(?P<name>authorization|proxy-authorization)\s*:\s*"
    r"(?P<value>(?:bearer|basic|token|apikey)\s+[A-Za-z0-9_\-\.=+/]{8,})",
    re.IGNORECASE,
)

#: A bare ``Bearer <credential>`` with no header name around it, which is how it
#: arrives when the header was split into a structure before being logged.
_AUTH_SCHEME = re.compile(
    r"\b(?P<scheme>bearer|basic)\s+(?P<value>[A-Za-z0-9_\-\.=+/]{16,})",
    re.IGNORECASE,
)

_URL_USERINFO = re.compile(
    r"\b(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<value>[^/\s:@'\"]+):[^/\s@'\"]+@"
)

#: Any URL. The internal-URL rule needs to look at the host before deciding, so
#: its rewrite returns the match unchanged for a public host — a rule may
#: therefore be present and match without redacting, which is why the mark is
#: recorded from the *replacement*, not from the match.
_URL = re.compile(
    r"\b(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://(?P<host>[^\s'\"<>/]{1,255})(?P<rest>[^\s'\"<>]*)?"
)

#: Service names that only exist inside a company network. Kept as prefixes
#: rather than a domain list because these are reached by short name.
_INTERNAL_SERVICE_PREFIXES = (
    "gitlab.",
    "jira.",
    "confluence.",
    "nexus.",
    "jenkins.",
    "sonar.",
    "wiki.",
)

_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

_HIGH_ENTROPY = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")

#: A dict key that *is* the name of a credential. Matched as a whole `_`/`-`
#: delimited segment so that ``max_tokens`` is not a token.
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:token|secret|password|passwd|api_?key|apikey|private_?key|"
    r"access_?key|client_?secret|authorization|credential|passphrase)(?:$|[_-])",
    re.IGNORECASE,
)

#: What each kind of mark means in terms of who may see the data. A mark that is
#: not listed defaults to SECRET: an unrecognised placeholder is not a licence
#: to lower the level.
_KIND_SENSITIVITY: dict[str, Sensitivity] = {
    "private-key": Sensitivity.SECRET,
    "token": Sensitivity.SECRET,
    "jwt": Sensitivity.SECRET,
    "assigned-secret": Sensitivity.SECRET,
    "auth-header": Sensitivity.SECRET,
    "auth-scheme": Sensitivity.SECRET,
    "url-userinfo": Sensitivity.SECRET,
    "high-entropy": Sensitivity.SECRET,
    "internal-url": Sensitivity.INTERNAL,
}


def _placeholder(kind: str) -> str:
    return f"{PLACEHOLDER_PREFIX}{kind}»"


def _is_placeholder(value: str) -> bool:
    return value.startswith(PLACEHOLDER_PREFIX) or value.startswith(FAILED_PREFIX)


def _entropy(value: str) -> float:
    """Shannon entropy per character. High randomness, not length, is the signal."""
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _fingerprint(salt: bytes, value: str) -> str:
    return hmac.new(salt, value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _as_salt(salt: bytes | str | None) -> bytes:
    if isinstance(salt, bytes):
        return salt
    if isinstance(salt, str):
        return salt.encode("utf-8")
    configured = os.environ.get("FORGEPILOT_REDACTION_SALT")
    if configured:
        return configured.encode("utf-8")
    # A random per-process salt still lets one correlate two occurrences of the
    # same value *within* a run, which is what the fingerprint is for. It cannot
    # correlate across runs, and it cannot be brute-forced by guessing a payload
    # — set FORGEPILOT_REDACTION_SALT to make fingerprints stable across runs.
    return secrets.token_bytes(32)


@dataclass(frozen=True, slots=True)
class Rule:
    """One recogniser.

    ``rewrite`` may keep part of the match (the header name, the assignment's
    key) and must return the match unchanged when it decides not to redact.
    ``value_group`` names the group holding the *sensitive value*, which is what
    gets fingerprinted and what idempotence is judged on — for most rules that
    is the whole match.
    """

    name: str
    pattern: re.Pattern[str]
    sensitivity: Sensitivity
    rewrite: Callable[[re.Match[str]], str] | None = None
    value_group: int = 0
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class RedactionMark:
    """One rule fired. Counts and fingerprints only — never a value."""

    kind: str
    count: int
    fingerprints: tuple[str, ...]
    sensitivity: Sensitivity


@dataclass(frozen=True, slots=True)
class RedactionReport:
    """What a pass did, for the audit event and for the caller to react to."""

    marks: tuple[RedactionMark, ...] = ()
    #: Exception type name when a detector failed, else "". A failure means the
    #: text was replaced by its degraded stub — never that raw text was kept.
    failed: str = ""
    failed_in: str = ""

    @property
    def applied(self) -> bool:
        return bool(self.marks)

    @property
    def degraded(self) -> bool:
        return bool(self.failed)

    @property
    def is_secret(self) -> bool:
        return any(mark.sensitivity is Sensitivity.SECRET for mark in self.marks)

    @property
    def sensitivity(self) -> Sensitivity:
        best = Sensitivity.PUBLIC
        for mark in self.marks:
            if mark.sensitivity.rank() > best.rank():
                best = mark.sensitivity
        return best

    def to_event_data(self, *, seq: int) -> dict[str, Any]:
        """The audit payload. Carries no value and no reversible reference.

        ``withheld`` records whether the event's payload was collapsed to a stub
        rather than kept in redacted form. A reader of the log needs to be able to
        tell "this turn happened and its text is here" from "this turn happened and
        everything it carried was withheld" — otherwise the two look identical.
        """
        data: dict[str, Any] = {
            "event": seq,
            "withheld": self.is_secret,
            "marks": [
                {
                    "kind": mark.kind,
                    "count": mark.count,
                    "sensitivity": str(mark.sensitivity),
                    "fingerprints": list(mark.fingerprints),
                }
                for mark in self.marks
            ],
        }
        if self.failed:
            data["failed"] = self.failed
            data["failed_in"] = self.failed_in
        return data


def collapse_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    """Replace every string in a payload with one stub, keeping its shape.

    At ``Sensitivity.SECRET`` the payload is presumed sensitive **as a whole**, not
    only at the span a rule matched (the D2 doctrine's "高敏感时降级只存元数据").
    A pasted private key is evidence of a payload that should not be kept, and the
    words around it — a hostname, a customer name, an internal path — are exactly
    what a redacted-token placeholder would have preserved.

    Two properties are deliberate:

    - **Keys and non-string fields survive.** Everything downstream looks payloads
      up by name (``data["text"]``, ``data["callId"]``), so a payload that lost its
      shape would break the conversation rather than protect it, and the failure
      would arrive one turn later than the mistake.
    - **The stub carries the size, not the content.** ``bytes=N`` is what makes
      "this message was withheld" diagnosable, and it is a coarse enough fact to be
      safe. The fingerprint that makes it *correlatable* lives in the audit event.
    """
    size = len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
    stub = f"{PLACEHOLDER_PREFIX}{SECRET_EVENT_KIND}:bytes={size}»"
    return _collapse(data, stub)


def _collapse(value: Any, stub: str) -> Any:
    if isinstance(value, str):
        return stub
    if isinstance(value, Mapping):
        return {key: _collapse(item, stub) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_collapse(item, stub) for item in value]
    return value


def _keep_assignment(match: re.Match[str]) -> str:
    return (
        f"{match.group('key')}{match.group('sep')}{match.group('quote')}"
        f"{_placeholder('assigned-secret')}{match.group('quote')}"
    )


def _keep_auth_header(match: re.Match[str]) -> str:
    return f"{match.group('name')}: {_placeholder('auth-header')}"


def _keep_auth_scheme(match: re.Match[str]) -> str:
    return f"{match.group('scheme')} {_placeholder('auth-scheme')}"


def _keep_userinfo(match: re.Match[str]) -> str:
    return f"{match.group('scheme')}{_placeholder('url-userinfo')}@"


def _maybe_high_entropy(match: re.Match[str]) -> str:
    if _entropy(match.group(0)) < 3.5:
        return match.group(0)
    return _placeholder("high-entropy")


def _is_private_address(host: str) -> bool:
    parts = host.split(".")
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        return False
    first, second = int(parts[0]), int(parts[1])
    return (
        first == 10
        or (first == 172 and 16 <= second <= 31)
        or (first == 192 and second == 168)
        or first == 127
    )


def _make_internal_url_rule(hosts: Sequence[str]) -> Rule:
    """Rule 6: the intranet, which is sensitive as a *shape*, not as a value.

    The default matches the host patterns a company network actually uses —
    ``*.corp.*``, ``*.internal``, ``*.local``, RFC1918 addresses, the usual
    service short names — plus whatever the deployment configures. When it fires
    the whole URL becomes the placeholder: the path is often the sensitive part
    (a ticket id, a project name), so keeping it would keep the disclosure.
    """

    def rewrite(match: re.Match[str]) -> str:
        raw_host = match.group("host")
        host = raw_host.rsplit("@", 1)[-1].split(":", 1)[0].lower()
        if not host:
            return match.group(0)
        internal = (
            "." not in host
            or host.endswith((".internal", ".local", ".corp", ".intranet"))
            or ".corp." in host
            or host.startswith(_INTERNAL_SERVICE_PREFIXES)
            or _is_private_address(host)
            or any(host == name or host.endswith("." + name) for name in hosts)
        )
        if not internal:
            return match.group(0)
        return f"{match.group('scheme')}://{_placeholder('internal-url')}"

    return Rule(
        name="internal-url",
        pattern=_URL,
        sensitivity=Sensitivity.INTERNAL,
        rewrite=rewrite,
    )


def default_rules(
    *,
    internal_hosts: Sequence[str] = (),
    high_entropy: bool = False,
) -> tuple[Rule, ...]:
    """The rule set, in the order it is applied.

    Order is load-bearing in three places. The private-key rule runs first so a
    PEM body is consumed whole instead of being shredded by the token rule. The
    auth rules run **before** the assignment rule, because
    ``authorization: Bearer xyz`` otherwise matches the assignment rule with
    ``Bearer`` as the "value" — which would redact the word and leave the
    credential itself in the text. And the high-entropy rule runs **last** so it
    can only see text the named rules did not already claim.
    """
    return (
        Rule("private-key", _PEM_BLOCK, Sensitivity.SECRET),
        Rule("token", _TOKEN, Sensitivity.SECRET),
        Rule("jwt", _JWT, Sensitivity.SECRET),
        Rule(
            "auth-header",
            _AUTH_HEADER,
            Sensitivity.SECRET,
            rewrite=_keep_auth_header,
            value_group=_AUTH_HEADER.groupindex["value"],
        ),
        Rule(
            "auth-scheme",
            _AUTH_SCHEME,
            Sensitivity.SECRET,
            rewrite=_keep_auth_scheme,
            value_group=_AUTH_SCHEME.groupindex["value"],
        ),
        Rule(
            "assigned-secret",
            _ASSIGNMENT,
            Sensitivity.SECRET,
            rewrite=_keep_assignment,
            value_group=_ASSIGNMENT.groupindex["value"],
        ),
        Rule(
            "url-userinfo",
            _URL_USERINFO,
            Sensitivity.SECRET,
            rewrite=_keep_userinfo,
            value_group=_URL_USERINFO.groupindex["value"],
        ),
        _make_internal_url_rule(internal_hosts),
        Rule(
            "high-entropy",
            _HIGH_ENTROPY,
            Sensitivity.SECRET,
            rewrite=_maybe_high_entropy,
            enabled=high_entropy,
        ),
    )


class Redactor:
    """Applies the rules. Pure: the same input always yields the same output."""

    def __init__(
        self,
        *,
        salt: bytes | str | None = None,
        internal_hosts: Iterable[str] = (),
        high_entropy: bool = False,
        rules: Sequence[Rule] | None = None,
    ) -> None:
        self._salt = _as_salt(salt)
        self._internal_hosts = tuple(
            sorted({host.strip().lower() for host in internal_hosts if host.strip()})
        )
        self._rules = (
            tuple(rules)
            if rules is not None
            else default_rules(internal_hosts=self._internal_hosts, high_entropy=high_entropy)
        )

    @property
    def rules(self) -> tuple[Rule, ...]:
        return self._rules

    def fingerprint(self, value: str) -> str:
        return _fingerprint(self._salt, value)

    # -- text -------------------------------------------------------------
    def redact_text(self, text: str) -> tuple[str, RedactionReport]:
        """Redact one string. Safe to call twice: the second pass changes nothing."""
        marks: dict[str, list[str]] = {}
        failures: list[tuple[str, str]] = []
        out = self._redact_text(text, "$", marks, failures)
        return out, self._report(marks, failures)

    # -- payloads ---------------------------------------------------------
    def redact_payload(self, data: Mapping[str, Any]) -> tuple[dict[str, Any], RedactionReport]:
        """Redact an event's ``data``.

        Keys are never rewritten: downstream code looks payloads up by key
        (``callId``, ``content``), so renaming one to a placeholder would trade a
        leak for a crash. A key that *names* a credential is handled the other
        way round — its value is the secret, so the value is what goes.
        """
        marks: dict[str, list[str]] = {}
        failures: list[tuple[str, str]] = []
        out = self._walk(dict(data), "$", marks, failures)
        return out, self._report(marks, failures)

    # -- internals --------------------------------------------------------
    def _report(
        self, marks: dict[str, list[str]], failures: list[tuple[str, str]]
    ) -> RedactionReport:
        frozen: list[RedactionMark] = []
        for kind, prints in marks.items():
            unique = tuple(dict.fromkeys(prints))
            frozen.append(
                RedactionMark(
                    kind=kind,
                    count=len(prints),
                    # Bounded: an audit event must not become the leak by way of
                    # a thousand fingerprints of one repeated value.
                    fingerprints=unique[:8],
                    sensitivity=_KIND_SENSITIVITY.get(kind, Sensitivity.SECRET),
                )
            )
        failed_in, failed = failures[0] if failures else ("", "")
        return RedactionReport(marks=tuple(frozen), failed=failed, failed_in=failed_in)

    def _degrade(self, text: str) -> str:
        return f"{FAILED_PREFIX}bytes={len(text.encode('utf-8'))},fp={self.fingerprint(text)}»"

    def _redact_text(
        self,
        text: str,
        path: str,
        marks: dict[str, list[str]],
        failures: list[tuple[str, str]],
    ) -> str:
        if not text:
            return text
        for rule in self._rules:
            if not rule.enabled:
                continue
            try:
                text = self._apply(rule, text, marks)
            except Exception as exc:  # a detector must never break the run
                failures.append((f"{path}:{rule.name}", type(exc).__name__))
                # Degrade this string and keep going: a sibling field may still
                # be redactable, and the caller is told which rule failed.
                return self._degrade(text)
        return text

    def _apply(self, rule: Rule, text: str, marks: dict[str, list[str]]) -> str:
        def replace(match: re.Match[str]) -> str:
            value = match.group(rule.value_group)
            if not value or _is_placeholder(value):
                # Already redacted. Redacting a placeholder again would turn a
                # stable, auditable marker into a fingerprint of the marker.
                return match.group(0)
            replacement = rule.rewrite(match) if rule.rewrite is not None else _placeholder(rule.name)
            if replacement == match.group(0):
                return replacement
            marks.setdefault(rule.name, []).append(self.fingerprint(value))
            return replacement

        return rule.pattern.sub(replace, text)

    def _walk(
        self,
        value: Any,
        path: str,
        marks: dict[str, list[str]],
        failures: list[tuple[str, str]],
    ) -> Any:
        if isinstance(value, str):
            return self._redact_text(value, path, marks, failures)
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                if (
                    isinstance(item, str)
                    and len(item) >= 4
                    and not _is_placeholder(item)
                    and _SENSITIVE_KEY.search(name)
                ):
                    # The key named the credential, so the value is the secret.
                    marks.setdefault("assigned-secret", []).append(self.fingerprint(item))
                    out[name] = _placeholder("assigned-secret")
                    continue
                out[name] = self._walk(item, f"{path}.{name}", marks, failures)
            return out
        if isinstance(value, (list, tuple)):
            return [
                self._walk(item, f"{path}[{index}]", marks, failures)
                for index, item in enumerate(value)
            ]
        return value


_DEFAULT_REDACTOR: Redactor | None = None

#: The deployment's own domains. A comma-separated list in the environment, which
#: is the same channel the salt and every other deployment secret already uses
#: (``config/env_file.py`` loads ``.env.local`` into it).
INTERNAL_HOSTS_ENV = "FORGEPILOT_INTERNAL_HOSTS"
HIGH_ENTROPY_ENV = "FORGEPILOT_REDACTION_HIGH_ENTROPY"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _internal_hosts_from_env() -> tuple[str, ...]:
    raw = os.environ.get(INTERNAL_HOSTS_ENV, "")
    return tuple(host.strip().lower() for host in raw.split(",") if host.strip())


def _high_entropy_from_env() -> bool:
    return os.environ.get(HIGH_ENTROPY_ENV, "").strip().lower() in _TRUTHY


def default_redactor() -> Redactor:
    """The process-wide gate. Redaction is on unless a caller injects its own.

    The deployment's internal-host list and the one noisy rule are read from the
    environment rather than threaded through every entry point. That is a
    deliberate choice about where a mistake is possible: if the list had to be
    passed to :class:`~forgepilot.runtime.session.Session` by each caller, then
    the caller that forgot is the one that writes the intranet URL into the log.
    The gate defaults to *on* for a caller that knows nothing about it.

    Cached, because the rules compile regexes and the write path runs constantly.
    """
    global _DEFAULT_REDACTOR
    if _DEFAULT_REDACTOR is None:
        _DEFAULT_REDACTOR = Redactor(
            internal_hosts=_internal_hosts_from_env(),
            high_entropy=_high_entropy_from_env(),
        )
    return _DEFAULT_REDACTOR


def reset_default_redactor() -> None:
    """Drop the cached gate so the next call re-reads the environment.

    Its consumer is a test that has just changed the environment, and a
    deployment that reloads ``.env.local`` without restarting.
    """
    global _DEFAULT_REDACTOR
    _DEFAULT_REDACTOR = None
