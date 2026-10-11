"""A sandbox that answers from a script — service ``sandbox``.

Distinct from :mod:`forgepilot.providers.sandbox.recording`, and the difference
is the whole point:

- ``recording`` **never executes and says so** (``executed=False``). It exists so
  a test can assert *what would have run*.
- this one **stands in for a sandbox that did execute**, answering from a script
  and reporting ``executed=True``. It runs nothing either, but a caller treating
  its output as a placeholder would be wrong — the output is the observation the
  test is built around.

Two doubles, two purposes, each honest about which it is. Collapsing them would
mean either a recording sandbox that lies about executing (which real models
already caught once) or a scripted one that cannot drive a repair loop.

It is a **test and demo double**. A deployment uses a real sandbox provider.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.sandbox import (
    SERVICE_NAME,
    ExecResult,
    SandboxEnforcement,
    SandboxExecutionPolicy,
)

__all__ = ["ScriptedSandboxPlugin", "ScriptedResult"]


@dataclass(frozen=True, slots=True)
class ScriptedResult:
    """One canned outcome."""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""

    @classmethod
    def failure(cls, output: str, *, exit_code: int = 1) -> ScriptedResult:
        return cls(exit_code=exit_code, stdout=output)


class ScriptedSandboxPlugin(PluginService):
    """Serves ``sandbox`` from a queue of canned results."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        # Defaults to `full` so exec-capable tools can be armed; a test about
        # weak enforcement sets it explicitly.
        self._enforcement = SandboxEnforcement(str(cfg.get("enforcement", "full")))
        self._script: deque[ScriptedResult] = deque()
        self._default = ScriptedResult(
            exit_code=int(cfg.get("exit_code", 0)), stdout=str(cfg.get("stdout", ""))
        )
        #: Every command that was asked for, in order.
        self.calls: list[tuple[str, ...]] = []
        #: Set when the script runs dry, so a test that under-scripts fails loudly
        #: instead of silently reading the default result.
        self.exhausted = False

    # -- scripting ---------------------------------------------------------
    def script(self, *results: ScriptedResult | int | str) -> ScriptedSandboxPlugin:
        for result in results:
            if isinstance(result, ScriptedResult):
                self._script.append(result)
            elif isinstance(result, int):
                self._script.append(ScriptedResult(exit_code=result))
            else:
                self._script.append(ScriptedResult(exit_code=1, stdout=str(result)))
        return self

    def reset(self) -> None:
        self._script.clear()
        self.calls.clear()
        self.exhausted = False

    @property
    def remaining(self) -> int:
        return len(self._script)

    # -- Sandbox -----------------------------------------------------------
    def probe(self) -> SandboxEnforcement:
        return self._enforcement

    async def run(
        self,
        argv: Sequence[str],
        *,
        policy: SandboxExecutionPolicy,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: str | None = None,
    ) -> ExecResult:
        self.calls.append(tuple(argv))

        if self._script:
            result = self._script.popleft()
        else:
            self.exhausted = True
            result = self._default

        return ExecResult(
            argv=tuple(argv),
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=1,
            timed_out=False,
            enforcement=self._enforcement,
            # It ran nothing — but it is standing in for one that did, and a
            # caller must be able to drive a repair loop off the output.
            executed=True,
        )
