"""Review mode: ordering, coverage, and whether a claim is actually backed.

The assertions worth keeping:

- the reading order is **not alphabetical** — that is the whole point of the mode;
- a claim citing evidence that does not exist lands in ``unverified`` with the
  reason, rather than passing as established;
- hunks nobody accounted for are **counted and named**, which is what separates
  this from a summary that merely looks thorough;
- the impact analysis **says which backend produced it**, because a text search
  and an AST parse are not the same evidence.
"""

from __future__ import annotations

import difflib
import json

import pytest

from forgepilot.orchestration.modes.review import prepare_review
from forgepilot.runtime.review import (
    EvidenceUniverse,
    analysis_backend,
    ast_available,
    build_narrative,
    compute_coverage,
    parse_diff,
    classify_noise,
    detect_renames,
    group_hunks,
    render_json,
    render_markdown,
    validate_narrative,
)
from forgepilot.seams.review import (
    AnalysisBackend,
    ChangeNarrative,
    ChangeRole,
    Claim,
    EvidenceKind,
    EvidenceRef,
    NoiseKind,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def make_diff(files: dict[str, tuple[str, str]]) -> str:
    """Build a unified diff from ``{path: (before, after)}``."""
    chunks: list[str] = []
    for path, (before, after) in files.items():
        chunks.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    return "".join(chunks)


MIGRATION_BEFORE = "CREATE TABLE accounts (id INTEGER);\n"
MIGRATION_AFTER = "CREATE TABLE accounts (id INTEGER, retry_limit INTEGER DEFAULT 3);\n"

LOGIN_BEFORE = "def retry_login(client):\n    attempts = 0\n    while True:\n        attempts += 1\n"
LOGIN_AFTER = "MAX = 3\n\n\ndef retry_login(client):\n    for _ in range(MAX):\n        pass\n"

ROUTES_BEFORE = "def login_route(request):\n    return retry_login(request.client)\n"
ROUTES_AFTER = "def login_route(request):\n    return retry_login(request.client, request.config)\n"

CONFIG_BEFORE = "retry: 5\n"
CONFIG_AFTER = "retry: 3\n"

TEST_BEFORE = "def test_retry():\n    assert True\n"
TEST_AFTER = "def test_retry_is_bounded():\n    assert retry_login(None) is None\n"

LOCK_BEFORE = '{"a": "1"}\n'
LOCK_AFTER = '{"a": "2"}\n'

FIVE_FILES = {
    "migrations/001_accounts.sql": (MIGRATION_BEFORE, MIGRATION_AFTER),
    "src/login.py": (LOGIN_BEFORE, LOGIN_AFTER),
    "src/api/routes.py": (ROUTES_BEFORE, ROUTES_AFTER),
    "config.yaml": (CONFIG_BEFORE, CONFIG_AFTER),
    "tests/test_login.py": (TEST_BEFORE, TEST_AFTER),
    "poetry.lock": (LOCK_BEFORE, LOCK_AFTER),
}


def universe_for(diff_text: str, *, high_water: int = 100) -> EvidenceUniverse:
    parsed = detect_renames(classify_noise(parse_diff(diff_text)))
    return EvidenceUniverse(
        high_water=high_water,
        changed_paths=parsed.paths,
        hunk_ids=[h.hunk_id for h in parsed.hunks],
        check_events=[7],
    )


# ---------------------------------------------------------------------------
# Parsing and noise
# ---------------------------------------------------------------------------
def test_a_diff_parses_into_files_and_hunks() -> None:
    parsed = parse_diff(make_diff(FIVE_FILES))

    assert set(parsed.paths) == set(FIVE_FILES)
    assert parsed.added > 0 and parsed.removed > 0


def test_lockfiles_and_generated_files_are_noise() -> None:
    diff = parse_diff(
        make_diff(
            {
                "poetry.lock": ('{"a":"1"}\n', '{"a":"2"}\n'),
                "src/api_pb2.py": ("x = 1\n", "x = 2\n"),
                "src/login.py": (LOGIN_BEFORE, LOGIN_AFTER),
            }
        )
    )
    classified = classify_noise(diff)

    by_path = {f.path: f for f in classified.files}
    assert by_path["poetry.lock"].noise is NoiseKind.LOCKFILE
    assert by_path["src/api_pb2.py"].noise is NoiseKind.GENERATED
    assert by_path["src/login.py"].noise is None


def test_a_whitespace_only_change_is_noise_but_is_still_present() -> None:
    """Folded, not deleted. A reformat is not interesting; its existence is."""
    diff = classify_noise(
        parse_diff(make_diff({"src/x.py": ("def f():\n    return 1\n", "def f():\n        return 1\n")}))
    )
    hunks = diff.hunks

    assert hunks and all(h.noise is NoiseKind.COSMETIC for h in hunks)
    assert len(diff.files) == 1, "the file is still in the diff, just classified"


def test_a_rename_is_one_change_not_a_deletion_plus_an_addition() -> None:
    """Otherwise a move reads as a wall of deletions plus a wall of additions."""
    body = "".join(f"line {i}\n" for i in range(40))
    diff = detect_renames(
        # `new_name.py` is the addition, `old_name.py` the deletion — same content.
        parse_diff(make_diff({"src/new_name.py": ("", body), "src/old_name.py": (body, "")}))
    )

    assert len(diff.files) == 1
    renamed = diff.files[0]
    assert renamed.path == "src/new_name.py"
    assert renamed.renamed_from == "src/old_name.py"


# ---------------------------------------------------------------------------
# Ordering — the reason this mode exists
# ---------------------------------------------------------------------------
def test_the_reading_order_is_not_alphabetical() -> None:
    """The finding this is built on: position predicts how much gets read.

    Alphabetical order would be config.yaml, migrations/…, poetry.lock,
    src/api/routes.py, src/login.py, tests/… — which puts configuration first and
    the actual logic fourth. The chosen order exists so a reviewer meets the data
    shape, then the logic, then its callers.
    """
    plan = prepare_review(make_diff(FIVE_FILES))
    roles = [g.role for g in plan.groups]
    paths = [p for g in plan.groups for p in g.paths]

    alphabetical = sorted(FIVE_FILES)
    assert paths != alphabetical, "if these match, the ordering is doing nothing"

    assert roles.index(ChangeRole.SCHEMA) < roles.index(ChangeRole.LOGIC)
    assert roles.index(ChangeRole.LOGIC) < roles.index(ChangeRole.API)
    assert roles.index(ChangeRole.API) < roles.index(ChangeRole.TESTS)
    assert roles[-1] is ChangeRole.NOISE, "noise belongs at the end, where it costs least"


def test_bigger_changes_come_before_smaller_ones_within_a_group() -> None:
    """Attention is freshest at the top; spend it on the larger change."""
    big_before = "".join(f"old {i}\n" for i in range(30))
    big_after = "".join(f"new {i}\n" for i in range(30))
    diff = make_diff(
        {
            "src/small.py": ("x = 1\n", "x = 2\n"),
            "src/big.py": (big_before, big_after),
        }
    )
    plan = prepare_review(diff)
    logic = next(g for g in plan.groups if g.role is ChangeRole.LOGIC)

    assert logic.paths[0] == "src/big.py"


def test_each_group_explains_why_it_sits_where_it_does() -> None:
    """Ordering a reviewer cannot explain is ordering they will not trust."""
    plan = prepare_review(make_diff(FIVE_FILES))

    assert all(g.position_reason for g in plan.groups)


# ---------------------------------------------------------------------------
# Evidence and validation
# ---------------------------------------------------------------------------
async def test_every_verified_claim_carries_evidence() -> None:
    narrative = await build_narrative(
        title="fix: bounded retry",
        **prepared(FIVE_FILES),
    )

    assert narrative.verified_claims, "the deterministic narrative should verify"
    for claim in narrative.verified_claims:
        assert claim.evidence, f"claim verified without evidence: {claim.text!r}"


async def test_a_claim_citing_a_nonexistent_event_goes_to_unverified() -> None:
    """The validator's whole job: a citation that does not resolve is not evidence."""
    narrative = await build_narrative(title="t", **prepared(FIVE_FILES))
    universe = universe_for(make_diff(FIVE_FILES), high_water=10)

    fabricated = Claim(
        text="这次改动修复了登录无限重试的问题。",
        evidence=(EvidenceRef(kind=EvidenceKind.SESSION_EVENT, session_seq=9999),),
    )
    validated = validate_narrative(
        ChangeNarrative(title="t", intent=fabricated, groups=narrative.groups), universe
    )

    assert not validated.intent.verified
    assert "does not exist" in validated.intent.unverified_reason
    assert any(c.text == fabricated.text for c in validated.unverified)


async def test_a_claim_citing_an_unchanged_file_is_rejected() -> None:
    """Citing code that is not part of this change evidences nothing about it."""
    from forgepilot.seams.refs import CodeRef, RepoRef

    universe = universe_for(make_diff(FIVE_FILES))
    claim = Claim(
        text="影响了配置加载。",
        evidence=(
            EvidenceRef(
                kind=EvidenceKind.CODE_REF,
                code_ref=CodeRef(repo=RepoRef(host="", namespace="", name=""), path="src/untouched.py", ref="HEAD"),
            ),
        ),
    )
    validated = validate_narrative(ChangeNarrative(title="t", intent=claim), universe)

    assert not validated.intent.verified
    assert "not part of this change" in validated.intent.unverified_reason


async def test_a_claim_with_no_evidence_is_never_verified() -> None:
    universe = universe_for(make_diff(FIVE_FILES))
    claim = Claim(text="性能有改善。")

    validated = validate_narrative(ChangeNarrative(title="t", intent=claim), universe)

    assert not validated.intent.verified
    assert validated.intent.unverified_reason
    assert validated.unverified, "it is listed, not silently dropped"


async def test_a_real_citation_verifies() -> None:
    """The counter-test: the validator is not simply rejecting everything."""
    diff_text = make_diff(FIVE_FILES)
    universe = universe_for(diff_text)
    hunk_id = next(iter(universe.hunk_ids))

    claim = Claim(
        text="有一个真实的改动块。",
        evidence=(EvidenceRef(kind=EvidenceKind.DIFF_HUNK, hunk_id=hunk_id),),
    )
    validated = validate_narrative(ChangeNarrative(title="t", intent=claim), universe)

    assert validated.intent.verified
    assert not validated.unverified


# ---------------------------------------------------------------------------
# Coverage — the honesty feature
# ---------------------------------------------------------------------------
def test_coverage_counts_hunks_no_claim_accounts_for() -> None:
    plan = prepare_review(make_diff(FIVE_FILES))
    coverage = compute_coverage(plan.parsed, plan.groups)

    assert coverage.total_hunks == len(plan.parsed.hunks)
    assert coverage.noise_hunks >= 1, "the lockfile is noise"
    # Every group rationale cites its own hunks, so the deterministic narrative
    # accounts for everything non-noise — which is exactly what it should claim.
    assert coverage.unexamined_hunks == 0


def test_unaccounted_hunks_are_named_not_just_counted() -> None:
    """A number tells a reviewer something is missing; a path tells them where."""
    from dataclasses import replace

    plan = prepare_review(make_diff(FIVE_FILES))
    stripped = tuple(replace(g, rationale=replace(g.rationale, evidence=())) for g in plan.groups)

    coverage = compute_coverage(plan.parsed, stripped)

    assert coverage.unexamined_hunks > 0
    assert coverage.unexamined, "paths, so a reviewer can go straight there"
    assert coverage.looked_at_ratio < 1.0


async def test_the_rendered_document_states_what_was_not_examined() -> None:
    narrative = await build_narrative(title="t", **prepared(FIVE_FILES))
    from dataclasses import replace

    stripped = tuple(
        replace(g, rationale=replace(g.rationale, evidence=())) for g in narrative.groups
    )
    partial = replace(narrative, groups=stripped)
    partial = replace(partial, coverage=compute_coverage(narrative_coverage_diff(), stripped))

    text = render_markdown(partial)

    assert "未审视" in text
    assert "没有人看过" in text or "没有任何论断" in text


def narrative_coverage_diff():
    return detect_renames(classify_noise(parse_diff(make_diff(FIVE_FILES))))


# ---------------------------------------------------------------------------
# Impact analysis: depth must be visible
# ---------------------------------------------------------------------------
def test_the_text_backend_says_so_and_warns_about_false_positives() -> None:
    """A grep and a parse are not the same evidence, and must not look alike."""
    if ast_available():
        pytest.skip("tree-sitter is installed, so the text fallback is not in play")

    plan = prepare_review(make_diff(FIVE_FILES), workspace_sources={"src/login.py": LOGIN_AFTER})

    assert plan.backend is AnalysisBackend.TEXT
    assert "文本匹配" in plan.blast_radius.note
    assert "假阳性" in plan.blast_radius.note


def test_removed_symbols_are_detected_and_their_callers_found() -> None:
    before = "def helper(x):\n    return x\n\n\ndef caller():\n    return helper(1)\n"
    after = "def caller():\n    return 1\n"
    plan = prepare_review(
        make_diff({"src/util.py": (before, after)}),
        workspace_sources={"src/util.py": after, "src/other.py": "from util import helper\n\nhelper(2)\n"},
    )

    names = {c.name for c in plan.blast_radius.symbols}
    assert "helper" in names
    assert any(s.kind.value == "removed" for s in plan.blast_radius.symbols)


@pytest.mark.skipif(
    not ast_available(),
    reason=(
        "tree-sitter is not installed: it publishes no cp314 wheel for Windows "
        "(win_amd64 stops at cp313, cp314 exists only for macOS/manylinux) and has "
        "no abi3 or pure-Python fallback, so it cannot be built without a C "
        "toolchain. Production is Linux, where the wheel exists."
    ),
)
def test_the_ast_backend_reports_itself_as_ast() -> None:
    plan = prepare_review(make_diff(FIVE_FILES))

    assert plan.backend is AnalysisBackend.AST
    assert plan.blast_radius.is_ast


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
async def test_markdown_and_json_come_from_one_narrative() -> None:
    """Two renderings of the same object, so they cannot tell different stories."""
    narrative = await build_narrative(title="fix: bounded retry", **prepared(FIVE_FILES))

    markdown = render_markdown(narrative)
    payload = render_json(narrative)

    assert "fix: bounded retry" in markdown
    assert payload["title"] == "fix: bounded retry"
    assert len(payload["groups"]) == len([g for g in narrative.groups])
    # Round-trips through JSON, so nothing in it is unserialisable.
    assert json.loads(json.dumps(payload))["title"] == "fix: bounded retry"


async def test_the_document_leads_with_intent_and_order_before_any_diff() -> None:
    """Information before it is needed: why, then how, then what to read."""
    narrative = await build_narrative(title="t", **prepared(FIVE_FILES))
    markdown = render_markdown(narrative)

    assert markdown.index("## 意图") < markdown.index("## 建议的阅读顺序")
    assert "不是按文件名" in markdown, "say that the order is deliberate"


async def test_the_json_carries_full_locations_for_the_ui() -> None:
    """The M10 client needs hover targets, not counts."""
    narrative = await build_narrative(title="t", **prepared(FIVE_FILES))
    payload = render_json(narrative)

    assert "affectedCallers" in payload["blastRadius"]
    assert "unexamined" in payload["coverage"]
    assert all("evidence" in c for c in payload["approach"])


def prepared(files: dict[str, tuple[str, str]]) -> dict:
    """The keyword arguments ``build_narrative`` needs for a raw diff."""
    diff_text = make_diff(files)
    plan = prepare_review(diff_text)
    return {
        "diff": plan.parsed,
        "groups": plan.groups,
        "blast_radius": plan.blast_radius,
        "use_model": False,
    }
