"""``forgepilot --boot`` says what it knows, and exits on what it found.

The defect these tests exist for is one line of output next to another: ``--boot``
printed ``workspace FAILED`` and then ``result: OK``, and exited **0**. Both halves
were true as written — the tree had resolved, the rows had been registered — and the
summary line disagreed with the row list it was summarising, which is the shape
``docs/notes/defect-shape.md`` collects.

The failure it hid was real and mine: a plugin whose ``apply`` raises does not raise
out of ``ctx.plugin(...)``. The kernel keeps the exception **on the fiber**, so a
loader that catches what registration raises, and reads the state for the rest,
learns *that* the row failed and never *why*. See
``tests/kernel/test_kernel_contract.py`` section 14 for the kernel half.

Both tests here are **host-independent by construction**, and deliberately so: the
box this was found on fails the ``workspace`` row for its own accident (``tempfile``'s
last resort is the repository root here), and a test that depended on that accident
would pass on Windows and mean nothing on the runner. So the failing row is made
failing *by its configuration*, and the pending row is made pending by an ``inject``
nothing provides.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from forgepilot.cli.main import main
from forgepilot.providers.llm.replay import ReplayLLMPlugin

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPO_ROOT / "configs"

WORKSPACE_PLUGIN = "forgepilot.providers.workspace.local:LocalTempWorkspacePlugin"


def write_patch(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "boot-patch.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def boot(capsys: pytest.CaptureFixture[str], patch: Path) -> tuple[int, str, str]:
    code = main(["--config-dir", str(CONFIGS), "--patch", str(patch), "--boot"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def row_line(out: str, row_id: str) -> str:
    for line in out.splitlines():
        if line.strip().startswith(row_id):
            return line
    raise AssertionError(f"no line for row {row_id!r} in:\n{out}")


def reason_line(out: str, row_id: str) -> str:
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(row_id):
            return lines[index + 1]
    raise AssertionError(f"no line for row {row_id!r} in:\n{out}")


# ---------------------------------------------------------------------------
# A row that fails to activate is a failure, and it says why
# ---------------------------------------------------------------------------
def test_a_row_that_fails_to_activate_fails_the_boot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The defect, pinned: exit 1, the row, and the kernel's own reason.

    The row is made to fail by configuration rather than by this host — a
    ``base_dir`` underneath a *file*, which no platform can create. That keeps the
    test honest on the Linux runner, where the box's own ``%TEMP%`` accident does
    not exist and the default profile boots green.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file, so nothing may be created inside it\n", encoding="utf-8")
    patch = write_patch(
        tmp_path,
        "- id: workspace\n"
        f"  plugin: {WORKSPACE_PLUGIN}\n"
        f"  config:\n    base_dir: {json.dumps(str(blocker / 'sub'))}\n",
    )

    code, out, _ = boot(capsys, patch)

    assert "FAILED" in row_line(out, "workspace")
    assert code == 1, out
    assert "result: 1 row(s) FAILED" in out
    # The reason, not just the state: pre-M15 this line did not exist and the
    # exception lived only on the fiber.
    assert reason_line(out, "workspace").strip() != "the kernel recorded no exception"
    assert "Error: " in reason_line(out, "workspace")


def test_a_row_whose_plugin_does_not_resolve_is_reported_the_same_way(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The *registration* half of the same report, which already worked.

    ``apply_row`` raises here instead of leaving the failure on a fiber, so this
    path exited 1 before M15 too — pinned so a future rewrite of ``_apply_rows``
    cannot trade one half of the report for the other. The row is pointed at a
    real module and a name it does not have, which is the ``--patch`` typo a human
    actually makes.
    """
    patch = write_patch(
        tmp_path,
        "- id: workspace\n"
        f"  plugin: {WORKSPACE_PLUGIN}\n"
        f"  config:\n    base_dir: {json.dumps(str(tmp_path / 'ws'))}\n"
        "- id: typo\n"
        "  plugin: forgepilot.providers.memory.sqlite:NoSuchPlugin\n",
    )

    code, out, _ = boot(capsys, patch)

    assert code == 1, out
    assert "FAILED" in row_line(out, "typo")
    assert "PluginResolutionError: " in reason_line(out, "typo")
    assert "result: 1 row(s) FAILED" in out


# ---------------------------------------------------------------------------
# PENDING is not a failure — and must not hang
# ---------------------------------------------------------------------------
def test_a_pending_row_is_reported_and_is_not_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the same coin.

    A fiber that declares an unavailable dependency stays ``PENDING`` forever — the
    kernel does not poll ``check`` (semantics #10), so nothing will ever resolve it.
    That is the fail-closed property working, so it is not a failure and must not
    fail the boot. It also must not be *awaited*: a loader that awaited every row it
    could not classify would wait for an activation that is never coming, and
    ``--boot`` would hang here instead of printing a line.

    ⚠️ A patch-level ``inject`` is applied to the resolved **class**
    (``config/patch.py::_as_plugin``), so this test mutates an imported class for as
    long as it runs; ``monkeypatch`` puts the attribute back afterwards, including
    removing one that did not exist before.
    """
    monkeypatch.setattr(ReplayLLMPlugin, "inject", None, raising=False)
    patch = write_patch(
        tmp_path,
        "- id: workspace\n"
        f"  plugin: {WORKSPACE_PLUGIN}\n"
        f"  config:\n    base_dir: {json.dumps(str(tmp_path / 'ws'))}\n"
        "- id: pending-probe\n"
        "  plugin: forgepilot.providers.llm.replay:ReplayLLMPlugin\n"
        "  inject: [never_provided_by_anything]\n"
        "  tier: trusted\n",
    )

    code, out, _ = boot(capsys, patch)

    assert "PENDING" in row_line(out, "pending-probe")
    assert code == 0, out
    assert "result: OK" in out


# ---------------------------------------------------------------------------
# A FORGEPILOT_TMP the process cannot use is refused before anything runs
# ---------------------------------------------------------------------------
def test_an_unusable_temp_root_is_named_and_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2, the variable, the path, and the reason — not a later unrelated failure.

    ``FORGEPILOT_TMP`` is proved with a real ``mkstemp`` rather than assumed from a
    successful ``mkdir``, because on this box ``%TEMP%``, ``C:\\Windows\\Temp`` and
    ``c:\\tmp`` all exist and all refuse. A pin that silently fails would move the
    error to whichever unrelated ``mkdtemp`` ran next.
    """
    blocker = tmp_path / "file-again"
    blocker.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setenv("FORGEPILOT_TMP", str(blocker / "sub"))

    code = main(["--config-dir", str(CONFIGS), "--boot"])

    captured = capsys.readouterr()
    assert code == 2
    assert "FORGEPILOT_TMP" in captured.err
    assert "cannot hold a temporary file" in captured.err


def test_main_puts_the_process_tempdir_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned global is restored on the way out.

    A CLI process would not notice either way; the test process does, and so does
    anything embedding ``main()``. ``--dump-config`` is used because it is the
    cheapest command that goes all the way through the dispatch.
    """
    pinned = tmp_path / "pinned"
    monkeypatch.setenv("FORGEPILOT_TMP", str(pinned))
    before = tempfile.tempdir

    code = main(["--config-dir", str(CONFIGS), "--dump-config"])

    capsys.readouterr()
    assert code == 0
    assert tempfile.tempdir == before
    assert pinned.is_dir(), "it was really used, not merely accepted"
