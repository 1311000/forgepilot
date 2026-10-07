"""A sandbox that records instead of executing — service ``sandbox``.

**It never spawns a process.** That is the entire point: a provider used by tests
must not be able to run arbitrary commands, or a test that thinks it is exercising
a refusal path could actually be executing something. Every call is recorded and
answered from a script, and ``probe()`` reports
:attr:`SandboxEnforcement.NONE` so that anything requiring real containment
refuses to run rather than assuming it is protected.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.sandbox import (
    SERVICE_NAME,
    ExecResult,
    SandboxEnforcement,
    SandboxExecutionPolicy,
)

__all__ = ["RecordingSandboxPlugin"]


@dataclass(frozen=True, slots=True)
class RecordedExec:
    argv: tuple[str, ...]
    cwd: Path
    policy: SandboxExecutionPolicy


class RecordingSandboxPlugin(PluginService):
    """Serves ``sandbox``; records calls, executes nothing."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        self._enforcement = SandboxEnforcement(str(cfg.get("enforcement", "none")))
        self._exit_code = int(cfg.get("exit_code", 0))
        self._stdout = str(cfg.get("stdout", ""))
        #: Every command that *would* have run. Public so a test can assert both
        #: that a command was attempted and that it was never executed.
        self.calls: list[RecordedExec] = []

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
        self.calls.append(RecordedExec(argv=tuple(argv), cwd=cwd, policy=policy))
        return ExecResult(
            argv=tuple(argv),
            exit_code=self._exit_code,
            stdout=self._stdout,
            stderr="",
            duration_ms=0,
            timed_out=False,
            enforcement=self._enforcement,
        )
