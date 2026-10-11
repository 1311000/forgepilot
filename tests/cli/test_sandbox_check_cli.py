"""``forgepilot --sandbox-check``: the question ``--boot`` does not answer.

``--boot`` reports that every row **loaded**. That is a different claim from "this
host can contain what the profile needs", and the gap between them is where finding
20 lived: the kernel-sandbox row loaded for as long as its boundary confined writes
and not reads, and nothing failed.

So these tests are mostly about **exit status**, because that is what a pipeline
reads. Two of them are host-independent by construction: a profile whose sandbox is
the *recording* double enforces nothing and must never pass, and one whose sandbox is
the *scripted* double reports ``full`` and must pass. Neither depends on the machine
running the suite, which is what makes them worth having on Windows as well as on
Linux.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgepilot.cli.main import main

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPO_ROOT / "configs"
KERNEL_SANDBOX = REPO_ROOT / "src" / "forgepilot" / "bundles" / "kernel_sandbox" / "patch.yaml"


def check(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str, str]:
    code = main(["--config-dir", str(CONFIGS), "--sandbox-check", *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def a_sandbox_patch(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sandbox-patch.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The failure mode the command exists to catch
# ---------------------------------------------------------------------------
def test_a_sandbox_that_enforces_nothing_does_not_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default profile ships the *recording* sandbox, which never executes.

    A check that reported success here would be re-running the mistake it was
    written to catch: "the row loaded" is not "the host is contained".
    """
    code, out, _ = check(capsys)

    assert code == 1
    assert "recording:RecordingSandboxPlugin" in out
    assert "probe        : none" in out
    assert "required     : full" in out
    assert "result       : FAILED" in out
    assert "requires 'full'" in out and "reports 'none'" in out


def test_a_sandbox_that_cannot_run_what_it_reports_is_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``full`` from ``probe()`` and a command that then fails is still not a pass.

    ``arm`` only asks the probe, so a boundary that reports ``full`` and refuses
    every command would hand out ``exec`` and fail one turn later — with the
    failure arriving somewhere that looks like the agent's problem.
    """
    patch = a_sandbox_patch(
        tmp_path,
        "- id: sandbox\n"
        "  plugin: forgepilot.providers.sandbox.scripted:ScriptedSandboxPlugin\n"
        "  config:\n"
        "    enforcement: full\n"
        "    exit_code: 1\n",
    )

    code, out, _ = check(capsys, "--patch", str(patch))

    assert code == 1
    assert "probe        : full" in out
    assert "shell        : ran, exit code 1" in out
    assert "could not be run through the boundary" in out


# ---------------------------------------------------------------------------
# The passing path — pinned on any host, so a regression is not mistaken for a machine
# ---------------------------------------------------------------------------
def test_a_host_that_can_contain_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``exit 0`` is the whole contract, and this is the only way to pin it here."""
    patch = a_sandbox_patch(
        tmp_path,
        "- id: sandbox\n"
        "  plugin: forgepilot.providers.sandbox.scripted:ScriptedSandboxPlugin\n"
        "  config:\n"
        "    enforcement: full\n",
    )

    code, out, _ = check(capsys, "--patch", str(patch))

    assert code == 0
    assert "probe        : full" in out
    assert "required     : full" in out
    assert "shell        : ran, exit code 0" in out
    assert "result       : OK" in out


def test_allowing_uncontained_exec_lowers_the_bar(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--allow-uncontained-exec`` has one meaning, and this command reuses it.

    The flag does not change what the sandbox *reports* — a backend that says
    ``none`` goes on saying ``none`` — it changes what the profile requires. So the
    check still prints ``none`` and the warning, and exits 0, which is the honest
    description of a deployment that has decided it does not need containment.
    """
    code, out, err = check(capsys, "--allow-uncontained-exec")

    assert code == 0
    assert "probe        : none" in out
    assert "required     : none" in out
    assert "result       : OK" in out
    assert "WITHOUT real containment" in err


# ---------------------------------------------------------------------------
# What it says about the boundary, not just whether it works
# ---------------------------------------------------------------------------
def test_the_check_names_the_row_and_the_claim_it_makes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`read_scope` is the difference between "confined" and "confined, as far as writes go".

    Finding 20 in one line: this row used to say nothing about reads while its
    boundary leaked every one of them, and a ``full`` from ``probe()`` could not
    tell the two apart. The claim is printed from the row's own config, and the
    verdict from ``probe()`` — the verification, not the promise.
    """
    code, out, _ = check(capsys, "--patch", str(KERNEL_SANDBOX))

    assert code in (0, 1), "this is host-dependent; the contract is checked elsewhere"
    assert "sandbox      : sandbox -> " in out
    assert "namespace:NamespaceSandboxPlugin" in out
    assert "claim        : read_scope: host" in out
    assert "probe        : " in out


def test_a_profile_without_a_sandbox_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No boundary at all is not a boundary that passed — and it is not exit 1 either.

    1 means "this host is weaker than the profile needs", which invites a retry or
    a fleet-wide shrug. A profile with no sandbox row cannot be contained by any
    host, so it is a configuration error: exit 2.
    """
    patch = a_sandbox_patch(tmp_path, "- id: sandbox\n  disabled: true\n")

    code, _, err = check(capsys, "--patch", str(patch))

    assert code == 2
    assert "no `sandbox` service" in err
