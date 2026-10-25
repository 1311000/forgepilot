"""The freshness exporter: what it prints, and what it refuses to assume.

Two rules are under test here beyond the arithmetic. A service that does not name a ref is
**stale** (that is `lag()`'s decision, not the exporter's), and a directory that is not the
root of a checkout is not that checkout — `git -C <dir> rev-parse HEAD` walks upward.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_tool():
    """Load `tools/index_freshness.py` by path — `tools/` is a directory, not a package.

    The module is registered in `sys.modules` *before* it executes: `@dataclass(slots=True)`
    resolves the defining module by name while the class body is being built, and an
    unregistered module makes it look for `None.__dict__`.
    """

    path = ROOT / "tools" / "index_freshness.py"
    spec = importlib.util.spec_from_file_location("index_freshness_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = load_tool()


def status(**overrides):
    from forgepilot.seams.index_service import IndexStatus

    fields = {"repositories": 1, "files": 10, "updated_at": "", "refs": (), "refs_by_repo": {}}
    fields.update(overrides)
    return IndexStatus(**fields)


def repo(name="app"):
    from forgepilot.seams.refs import RepoRef

    return RepoRef(host="git.corp", namespace="team", name=name)


def a_checkout(tmp_path: Path, commits: int = 2) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    run(root, "init", "-q", "-b", "main")
    run(root, "config", "user.email", "t@example.com")
    run(root, "config", "user.name", "t")
    for index in range(commits):
        (root / f"f{index}.txt").write_text(f"{index}\n", encoding="utf-8")
        run(root, "add", "-A")
        run(root, "commit", "-q", "-m", f"c{index}")
    return root


def run(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_parse_repo_reads_the_three_part_slug_and_the_path():
    parsed, path = tool.parse_repo("git.corp/team/app=/srv/app")
    assert parsed.slug == "git.corp/team/app"
    assert str(path).endswith("app")


@pytest.mark.parametrize("value", ["nope=/srv/app", "git.corp/team=/srv/app", "git.corp/team/app="])
def test_parse_repo_refuses_a_slug_it_cannot_read(value):
    with pytest.raises(ValueError):
        tool.parse_repo(value)


def test_age_is_zero_when_the_service_does_not_say():
    """Unknown is a gap, not an ancient corpus: an alert on a missing field is noise."""

    assert tool.age_seconds("") == 0.0
    assert tool.age_seconds("not a date") == 0.0


def test_age_is_measured_from_the_reported_timestamp():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    stamp = (now - timedelta(hours=3)).isoformat()
    assert tool.age_seconds(stamp, now=now) == pytest.approx(3 * 3600)


def test_a_naive_timestamp_is_read_as_utc():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    assert tool.age_seconds("2026-10-25T06:00:00", now=now) == pytest.approx(6 * 3600)


def test_a_fresh_checkout_measures_zero(tmp_path):
    root = a_checkout(tmp_path, commits=2)
    head = tool.head_of(root)
    measured = tool.measure(status(refs_by_repo={repo().slug: head}), {repo(): root})
    assert measured[0].behind_commits == 0
    assert measured[0].known


def test_a_checkout_ahead_of_the_index_measures_the_gap(tmp_path):
    root = a_checkout(tmp_path, commits=3)
    behind_head = run(root, "rev-parse", "HEAD~2")
    measured = tool.measure(status(refs_by_repo={repo().slug: behind_head}), {repo(): root})
    assert measured[0].behind_commits == 2


def test_a_service_that_names_no_ref_is_stale_not_fresh(tmp_path):
    root = a_checkout(tmp_path, commits=1)
    measured = tool.measure(status(), {repo(): root})
    assert measured[0].behind_commits == 1
    assert not measured[0].known


def test_a_directory_that_is_not_a_checkout_root_is_not_measured_as_one(tmp_path):
    root = a_checkout(tmp_path, commits=1)
    nested = root / "sub"
    nested.mkdir()
    measured = tool.measure(status(), {repo(): nested})
    assert measured[0].behind_commits == 1
    assert not measured[0].known


def test_render_names_the_two_series_the_alerts_use():
    text = tool.render(
        [tool.Measurement("git.corp/team/app", "head", "indexed", 2, True)],
        age=90.0,
        indexed_refs=("indexed",),
    )
    assert 'forgepilot_index_lag_commits{repo="git.corp/team/app",indexed="indexed"} 2' in text
    assert "forgepilot_index_age_seconds 90" in text
    assert "forgepilot_index_refs 1" in text


def test_the_cli_fails_when_a_repository_is_behind(tmp_path, monkeypatch, capsys):
    from forgepilot.seams.index_service import IndexStatus

    root = a_checkout(tmp_path, commits=2)

    class FakeService:
        def __init__(self, *args, **kwargs):
            pass

        async def status(self, constraints):
            return IndexStatus(repositories=1, files=1, updated_at="", refs_by_repo={})

    monkeypatch.setattr(tool, "HttpIndexService", FakeService)
    code = tool.main(["--index-url", "http://index.invalid", "--repo", f"git.corp/team/app={root}"])
    assert code == 1
    assert "behind" in capsys.readouterr().err


def test_the_cli_prints_metrics_on_stdout_and_prose_on_stderr(tmp_path, monkeypatch, capsys):
    from forgepilot.seams.index_service import IndexStatus

    root = a_checkout(tmp_path, commits=1)
    head = tool.head_of(root)

    class FakeService:
        def __init__(self, *args, **kwargs):
            pass

        async def status(self, constraints):
            return IndexStatus(
                repositories=1, files=1, updated_at="", refs_by_repo={"git.corp/team/app": head}
            )

    monkeypatch.setattr(tool, "HttpIndexService", FakeService)
    code = tool.main(["--index-url", "http://index.invalid", "--repo", f"git.corp/team/app={root}"])
    out = capsys.readouterr()
    assert code == 0
    assert "forgepilot_index_lag_commits" in out.out
    assert "fresh" in out.err
    assert "forgepilot_" not in out.err


def test_the_cli_json_form_carries_the_same_numbers(tmp_path, monkeypatch, capsys):
    from forgepilot.seams.index_service import IndexStatus

    root = a_checkout(tmp_path, commits=3)
    behind_head = run(root, "rev-parse", "HEAD~1")

    class FakeService:
        def __init__(self, *args, **kwargs):
            pass

        async def status(self, constraints):
            return IndexStatus(
                repositories=1,
                files=1,
                updated_at="",
                refs_by_repo={"git.corp/team/app": behind_head},
            )

    monkeypatch.setattr(tool, "HttpIndexService", FakeService)
    code = tool.main(
        ["--index-url", "http://index.invalid", "--repo", f"git.corp/team/app={root}", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["repositories"][0]["lagCommits"] == 1


def test_an_unreachable_index_is_exit_two(tmp_path, monkeypatch, capsys):
    root = a_checkout(tmp_path, commits=1)

    class Refusing:
        def __init__(self, *args, **kwargs):
            pass

        async def status(self, constraints):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(tool, "HttpIndexService", Refusing)
    code = tool.main(["--index-url", "http://index.invalid", "--repo", f"git.corp/team/app={root}"])
    assert code == 2
    assert "did not answer" in capsys.readouterr().err
