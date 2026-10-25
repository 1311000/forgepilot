"""P33: two real processes, one state directory — the lease and the log across a boundary.

P16 proved the coordination seam in one interpreter with two `SessionRegistry` objects in it.
That is weaker than it looks (one process, one event loop, one set of live objects), so this
test starts **two servers** — separate processes, separate memory — sharing a state directory,
and drives the scenario `tools/two_replicas.py` describes. It runs in the ordinary gate, which
means the Linux CI job is the end-to-end proof.

The two defects this scenario found on its first run are worth naming, because both were
invisible to the in-process tests:

* the `coordination` bundle could not load at all (a `patches:` mapping without `entries:`, and a
  plugin class with no `provide`), so a deployment following the documentation would have failed
  at boot rather than run without a lease;
* `FileCoordination.holder` returned a *string* where the seam declares `Lease | None`, which no
  test noticed because every caller only asked "is it None".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    """Load ``tools/two_replicas.py`` as a module: ``tools/`` is a script directory, not a package."""
    name = "two_replicas_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "two_replicas.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_two_processes_share_one_log_and_one_lease(tmp_path: Path) -> None:
    two = _module()
    state = tmp_path / "state"
    workspaces = state / "work"
    state.mkdir(parents=True)
    workspaces.mkdir()
    patch = two.write_patch(state)

    processes: list = []
    bases: list[str] = []
    try:
        for _ in range(2):
            port = two.free_port()
            bases.append(f"http://127.0.0.1:{port}")
            processes.append(
                two.start(root=ROOT, state=state, workspaces=workspaces, port=port, patch=patch)
            )
        for base, process in zip(bases, processes, strict=True):
            two.wait_healthy(base, process=process, timeout_s=90)
        checks = await two.scenario(bases[0], bases[1], timeout_s=90)
    finally:
        for process in processes:
            two._stop(process)

    failed = [check for check in checks if not check.ok]
    assert not failed, "\n".join(f"{check.name}: {check.detail}" for check in failed)
    assert len(checks) == 7
