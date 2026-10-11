"""The eval prints UTF-8, including on a console that is not.

⚠️ **Found by the report dying after it had already printed everything useful.**

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u26a0' in position 0

An em dash had been arriving as `��` for as long as the report has existed and
nobody minded, because it looked like a terminal font problem. A `⚠️` does not
render as anything — it raises. And it raised at the *end*, so the table, the
matrix and every failure reason were on screen and the exit code was 1.

This is the CLI's defect again, in a second program: `_use_utf8_output` was written
for `forgepilot --task` and never shared, which is the same "a correction at one
call site has not been made" shape the CLI's own docstring warns about.

Tested by **running the real command with a GBK stdout**, because that is the only
thing that reproduces it — `capsys` hands back a stream that reconfigure does
nothing to, so a test through it would pass on the broken version.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
FORGE = HERE.parents[2]


def run_with_gbk(*args: str) -> subprocess.CompletedProcess:
    """Run an eval entry point with its stdout forced to GBK."""
    env = dict(os.environ)
    # What a Windows pipe does on its own, made deterministic.
    env["PYTHONIOENCODING"] = "gbk"
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(FORGE),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def write_results(tmp_path: Path) -> Path:
    """A file with one failure in it, so the report prints its warning as well."""
    from forgepilot.console import use_utf8_output  # noqa: F401  (import check)

    path = tmp_path / "results.jsonl"
    path.write_text(
        json.dumps(
            {
                "task": "t",
                "arm": "arm",
                "rep": 1,
                "run": "r1",
                "mode": "live",
                "passed": False,
                "failures": ["checks did not pass"],
                "state": "failed",
                "tool_calls": 1,
                "turns": 1,
                "wall_s": 1.0,
                "agent_text": "⚠️ a warning glyph the console cannot encode",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_the_report_survives_a_gbk_console(tmp_path: Path) -> None:
    path = write_results(tmp_path)

    done = run_with_gbk("evals/report.py", "--file", str(path), "--agent-text")

    assert done.returncode == 0, (
        f"the report exited {done.returncode} on a GBK console:\n{done.stderr[-2000:]}"
    )
    assert "UnicodeEncodeError" not in done.stderr
    # The content has to survive too — exiting cleanly by printing nothing would
    # also pass the two assertions above.
    assert "Why the runs that did not pass" in done.stdout
    assert "checks did not pass" in done.stdout


def test_the_helper_does_what_the_two_subprocess_tests_rely_on() -> None:
    """The mechanism, asserted directly — and the failure it prevents, asserted too.

    ⚠️ A first version of this test ran `run.py --help` and asserted exit 0. It
    passed on the **broken** version, because `--help` prints no glyph: it was
    checking that a thing with nothing to print printed nothing. `run.py`'s own call
    is defensive — no run of it has yet printed a character the console cannot
    encode, unlike the report, which printed one and died.
    """
    import io
    import sys as _sys

    from forgepilot.console import use_utf8_output

    def gbk_buffer() -> tuple[io.TextIOWrapper, io.BytesIO]:
        raw = io.BytesIO()
        return io.TextIOWrapper(raw, encoding="gbk"), raw

    # What happens without it — the crash this file exists for.
    plain, _ = gbk_buffer()
    with pytest.raises(UnicodeEncodeError):
        plain.write("⚠️")

    # And with it.
    stream, raw = gbk_buffer()
    saved = _sys.stdout
    try:
        _sys.stdout = stream
        use_utf8_output()
        stream.write("⚠️ ok")
        stream.flush()
    finally:
        _sys.stdout = saved
    assert b"ok" in raw.getvalue()


def test_the_helper_is_shared_rather_than_copied() -> None:
    """⚠️ **The point of the fix.** A private copy in the CLI is what left the eval
    printing mojibake for weeks; a second private copy here would do it again.

    Asserted by importing it from the package and by checking that the CLI no longer
    defines its own — the duplicate is the defect, not the behaviour.
    """
    from forgepilot.console import use_utf8_output

    assert callable(use_utf8_output)

    cli = (FORGE / "src" / "forgepilot" / "cli" / "main.py").read_text(encoding="utf-8")
    assert "def _use_utf8_output" not in cli, (
        "the CLI has grown its own copy again; use forgepilot.console"
    )
