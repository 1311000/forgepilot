"""The redaction rules, and the three ways a detector can fail.

A detector fails loudly (it raises) or quietly. The loud case is handled: the
text becomes a metadata stub and the run continues. The quiet cases are what most
of this file is about — a rule that misses a real credential leaks, and a rule
that fires on ordinary identifiers gets switched off within a week, which leaks
everything afterwards. ``docs/notes/defect-shape.md`` records that shape often
enough that "it looked enforced" is not an acceptable ending here.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from forgepilot.runtime.redaction import (
    FAILED_PREFIX,
    HIGH_ENTROPY_ENV,
    INTERNAL_HOSTS_ENV,
    PLACEHOLDER_PREFIX,
    Redactor,
    Rule,
    default_redactor,
    reset_default_redactor,
)
from forgepilot.seams.security import Sensitivity


@pytest.fixture(autouse=True)
def _fresh_default_redactor():
    """The cached gate must not carry one test's environment into the next."""
    reset_default_redactor()
    yield
    reset_default_redactor()


#: One sample per shipped rule, with the substring that must not survive.
#: Real-looking values on purpose: the rules match *shapes and prefixes*, so a
#: placeholder sample would test nothing.
SAMPLES = {
    "private-key": (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEA",
    ),
    "token": ("the deploy key is ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "ghp_ABCDEF"),
    "jwt": (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        "eyJhbGciOiJIUzI1NiJ9",
    ),
    "assigned-secret": ("API_TOKEN=abcdefghijklmnop", "abcdefghijklmnop"),
    "auth-header": (
        "authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "abcdefghijklmnopqrstuvwxyz",
    ),
    "url-userinfo": ("https://svc:s3cr3t-passw0rd@git.example.com/repo.git", "s3cr3t-passw0rd"),
    "internal-url": (
        "see http://nexus/repository/internal for the artifact",
        "repository/internal",
    ),
}

#: Text a rule must leave alone. Each line is here because a plausible detector
#: would fire on it: ``max_tokens`` contains "token", a GitHub URL contains a
#: host, and a short hex id looks like a key if you only measure length.
INNOCENT = (
    "max_tokens=200000 and max_wall_seconds=1800.0",
    "git clone https://github.com/1311000/forgepilot.git",
    "the file is src/forgepilot/runtime/redaction.py",
    "HUMAN_VERIFIED > TEST_VERIFIED > TOOL_OBSERVED > MODEL_ASSERTED",
    "commit 565ff1f and run 38140873105",
    "the secretary password policy is documented",
)


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_every_shipped_rule_removes_the_value_and_names_the_kind(kind: str) -> None:
    sample, needle = SAMPLES[kind]

    text, report = Redactor().redact_text(sample)

    assert needle not in text, "the value survived the rule that exists for it"
    assert f"{PLACEHOLDER_PREFIX}{kind}»" in text
    assert [mark.kind for mark in report.marks] == [kind]
    assert report.applied
    expected = Sensitivity.INTERNAL if kind == "internal-url" else Sensitivity.SECRET
    assert report.sensitivity is expected


@pytest.mark.parametrize("innocent", INNOCENT)
def test_ordinary_text_is_not_mangled(innocent: str) -> None:
    text, report = Redactor().redact_text(innocent)

    assert text == innocent, "the gate rewrote something it had no business touching"
    assert report.marks == ()
    assert not report.applied


def test_a_second_pass_changes_nothing() -> None:
    """Idempotence, because a redacted event is re-read (recall, export, replay)."""
    once, first = Redactor().redact_text(SAMPLES["token"][0])
    twice, second = Redactor().redact_text(once)

    assert twice == once
    assert first.applied and not second.applied, "the placeholder was treated as a value"


def test_the_fingerprint_is_salted_and_stable() -> None:
    left = Redactor(salt=b"pepper")
    right = Redactor(salt=b"pepper")

    assert left.fingerprint("abc") == right.fingerprint("abc")
    assert left.fingerprint("abc") != Redactor(salt=b"other").fingerprint("abc")
    bare = hashlib.sha256(b"abc").hexdigest()[:16]
    assert left.fingerprint("abc") != bare, "an unsalted digest can be brute-forced"


def test_two_occurrences_share_a_fingerprint_and_one_mark() -> None:
    text, report = Redactor(salt=b"pepper").redact_text(
        f"{SAMPLES['token'][0]} / {SAMPLES['token'][0]}"
    )

    assert len(report.marks) == 1
    mark = report.marks[0]
    assert mark.count == 2
    assert len(mark.fingerprints) == 1, "the same value must not look like two different ones"


def test_the_audit_payload_carries_no_value() -> None:
    _, report = Redactor().redact_text(SAMPLES["token"][0])

    data = report.to_event_data(seq=7)
    blob = json.dumps(data)

    assert data["event"] == 7
    assert data["marks"][0]["kind"] == "token"
    assert data["marks"][0]["fingerprints"][0]
    assert "ghp_" not in blob and "ABCDEF" not in blob


def test_an_internal_url_is_redacted_whole_and_a_public_one_is_not() -> None:
    redactor = Redactor()

    public = "clone https://github.com/1311000/forgepilot.git now"
    assert redactor.redact_text(public)[0] == public

    for url in (
        "https://gitlab.corp.example/team/repo",
        "http://nexus/repository/internal",
        "https://10.1.2.3/dashboard",
        "https://wiki.internal/page",
    ):
        text, report = redactor.redact_text(f"see {url} for it")
        assert url not in text, url
        assert report.sensitivity is Sensitivity.INTERNAL


def test_a_configured_host_counted_as_internal() -> None:
    redactor = Redactor(internal_hosts=("forge.acme.io",))

    text, report = redactor.redact_text("https://forge.acme.io/x/y")

    assert "forge.acme.io" not in text
    assert report.sensitivity is Sensitivity.INTERNAL


def test_high_entropy_is_the_one_switch_and_it_defaults_to_off() -> None:
    """A gate that can be switched off in config is off in practice; this rule is
    noisy by nature, so it is the single exception, and it ships disabled."""
    blob = "Zx9Qw3Er7Ty1Up2As4Df6Gh8Jk0Lm5Nc"
    off, _ = Redactor().redact_text(f"checksum {blob}")
    on, report = Redactor(high_entropy=True).redact_text(f"checksum {blob}")

    assert off == f"checksum {blob}"
    assert blob not in on and report.applied


def test_the_only_disabled_rule_is_high_entropy() -> None:
    disabled = [rule.name for rule in Redactor().rules if not rule.enabled]

    assert disabled == ["high-entropy"]


def test_a_broken_detector_degrades_instead_of_leaking() -> None:
    def boom(match: re.Match[str]) -> str:
        raise ValueError("detector bug")

    broken = Rule(
        name="broken",
        pattern=re.compile(r"(ghp_\w+)"),
        sensitivity=Sensitivity.SECRET,
        rewrite=boom,
        value_group=1,
    )
    text, report = Redactor(rules=(broken,)).redact_text(f"key {SAMPLES['token'][0]}")

    assert report.degraded
    assert report.failed == "ValueError"
    assert text.startswith(FAILED_PREFIX)
    assert "ghp_" not in text, "the degraded stub kept the value"
    assert report.marks == ()


def test_the_walk_redacts_values_in_structures_and_leaves_other_types_alone() -> None:
    data = {
        "callId": "c1",
        "arguments": '{"command": "curl -H \'authorization: Bearer abcdefghijklmnopqrst\' http://nexus/x"}',
        "API_KEY": "abcd1234efgh",
        "max_tokens": 200_000,
        "content": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
        "nested": {"password": "hunter2xyz", "note": "ok"},
        "count": 3,
        "flag": True,
        "nothing": None,
        "list": ["plain", "token=abcdefghijklmnop"],
    }

    redacted, report = Redactor().redact_payload(data)
    dumped = json.dumps(redacted)

    # keys are never rewritten — downstream code looks payloads up by key
    assert set(redacted) == set(data)
    assert redacted["callId"] == "c1"
    assert redacted["count"] == 3
    assert redacted["flag"] is True
    assert redacted["nothing"] is None
    assert redacted["max_tokens"] == 200_000, "a budget number is not a credential"
    assert redacted["nested"]["note"] == "ok"
    # …and a key that *names* a credential has its value replaced
    assert redacted["API_KEY"] == f"{PLACEHOLDER_PREFIX}assigned-secret»"
    assert redacted["nested"]["password"] == f"{PLACEHOLDER_PREFIX}assigned-secret»"
    # nothing sensitive survives anywhere in the structure
    for needle in (
        "abcd1234efgh",
        "hunter2xyz",
        "abcdefghijklmnopqrst",
        "MIIB",
        "abcdefghijklmnop",
    ):
        assert needle not in dumped, needle
    assert report.applied
    assert {mark.kind for mark in report.marks} == {
        "assigned-secret",
        "auth-header",
        "internal-url",
        "private-key",
    }


def test_redaction_is_on_for_the_object_the_runtime_reaches_for() -> None:
    text, report = default_redactor().redact_text(SAMPLES["token"][0])

    assert "ghp_" not in text
    assert report.applied


def test_the_deployment_can_name_its_own_internal_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A company's own domain is not a shape any rule can guess.

    The defaults cover the shapes everyone shares (``*.internal``, RFC1918, a
    bare hostname). Everything else is deployment knowledge, so it enters through
    the environment rather than through a literal in this file.
    """
    monkeypatch.setenv(INTERNAL_HOSTS_ENV, "forge.acme.io, wiki.acme.io")
    reset_default_redactor()

    text, report = default_redactor().redact_text(
        "see https://forge.acme.io/x and https://example.com/y"
    )

    assert "forge.acme.io" not in text
    assert "example.com" in text, "a public host is not the deployment's business"
    assert [mark.kind for mark in report.marks] == ["internal-url"]


def test_the_high_entropy_rule_is_off_unless_the_deployment_turns_it_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one rule whose false-positive rate is unknown, so it is opt-in.

    It is the only switch that exists: a gate that can be turned off wholesale is
    off in practice, so the other rules have no ``enabled`` flag to flip.
    """
    opaque = "Zx9Qw3Er7Ty1Up2As4Df6Gh8Jk0Lm5Nc"
    monkeypatch.delenv(HIGH_ENTROPY_ENV, raising=False)
    reset_default_redactor()
    untouched, quiet = default_redactor().redact_text(f"digest {opaque}")
    assert opaque in untouched and not quiet.applied

    monkeypatch.setenv(HIGH_ENTROPY_ENV, "true")
    reset_default_redactor()
    text, report = default_redactor().redact_text(f"digest {opaque}")

    assert opaque not in text
    assert [mark.kind for mark in report.marks] == ["high-entropy"]


def test_a_new_environment_change_is_picked_up_by_resetting_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reset_default_redactor`` has a consumer, so it is not dead code.

    Without it the first call would freeze the rules for the life of the process,
    and a deployment that reloads ``.env.local`` would keep the old ones.
    """
    monkeypatch.setenv(INTERNAL_HOSTS_ENV, "wiki.acme.io")
    reset_default_redactor()
    first = default_redactor()
    assert default_redactor() is first, "the gate is cached; it compiles regexes"

    monkeypatch.setenv(INTERNAL_HOSTS_ENV, "other.acme.io")
    reset_default_redactor()

    assert default_redactor() is not first
    text, _ = default_redactor().redact_text("https://other.acme.io/x")
    assert "other.acme.io" not in text
