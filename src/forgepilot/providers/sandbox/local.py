"""A sandbox that really runs commands, with no containment — service ``sandbox``.

**This backend exists to close the loop, not to make it safe.** Until it did, the
`run` tool refused on every platform (the recording sandbox never executes, and
`arm` requires `full` enforcement for `exec`), so every `task/check` was
`could_not_run` and the harness could edit files but never establish that an edit
was right. The repair loop looked like it worked only because tests fed it a
scripted exit code.

What it does: spawn the command in the workspace, capture its output, hold a
timeout, report its real exit code.

What it does **not** do: restrict the filesystem, restrict the network, restrict
process creation, or hide anything on the host. A command run here can read the
whole machine. That is not a shortcoming to be papered over — it is why

- :meth:`probe` returns :attr:`SandboxEnforcement.NONE` **unconditionally**, and
  why that value is not configurable. `probe()` is documented as "a fact about
  the machine, not a preference"; a knob that let an operator set `full` here
  would turn the one honest signal in the sandbox stack into a formality, and
  `arm`'s refusal to run `exec` below `full` is built on it. Use
  :mod:`forgepilot.providers.sandbox.bubblewrap` where real containment is
  available;
- `arm` refuses `exec` on a host reporting this, so reaching this backend's
  execution path is a **deliberate, visible act** (`--allow-uncontained-exec`),
  not a default.

Execution is dispatched through a worker thread rather than asyncio's subprocess
API, which on Windows additionally requires a Proactor event loop — a detail that
would make this provider behave differently under pytest than under the CLI.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from forgepilot.kernel import PluginService
from forgepilot.seams.sandbox import (
    SERVICE_NAME,
    ExecResult,
    PolicyNotEnforceable,
    SandboxEnforcement,
    SandboxExecutionPolicy,
    SandboxMode,
)
from forgepilot.seams.security import NetworkAccess

__all__ = ["LocalProcessSandboxPlugin", "PolicyNotEnforceable"]

#: Conventional shell status for "command not found". Reported rather than
#: raised, so a typo in `project.yaml` reads as a failed check with a reason.
NOT_FOUND = 127
#: Status reported for a command that had to be killed. Non-zero on purpose:
#: "it hung" and "it passed" must never be the same result.
KILLED = -1


class LocalProcessSandboxPlugin(PluginService):
    """Serves ``sandbox``; executes for real, enforces nothing."""

    provide = SERVICE_NAME

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        cfg = dict(config or {})
        #: Per-stream cap. A command that prints a gigabyte must not become a
        #: gigabyte in the session log.
        self.max_output_bytes = int(cfg.get("max_output_bytes", 64_000))

    def probe(self) -> SandboxEnforcement:
        """Always ``NONE``.

        Deliberately not read from config. A backend cannot report containment it
        does not apply, and the one thing this provider must never do is make the
        sandbox stack's weakest link look strong.
        """
        return SandboxEnforcement.NONE

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
        argv = tuple(str(a) for a in argv)
        started = time.monotonic()

        # Refused *before* anything is spawned. Refusing afterwards would be
        # worse than not refusing: the effect would already have happened.
        #
        # `mode` used to be accepted and never read, so a caller asking for
        # read-only got a command free to write anywhere. Nothing asked for it,
        # which is why it was harmless — and why it would have stayed harmless
        # right up until someone relied on it.
        if policy.mode is SandboxMode.READ_ONLY:
            raise PolicyNotEnforceable(
                f"this backend provides no containment, so it cannot honour a "
                f"{policy.mode.value!r} policy — a command run here may write anywhere. "
                f"Refusing rather than running something other than what was asked "
                f"for. Use a backend that reports `full`, or ask for "
                f"{SandboxMode.WORKSPACE_WRITE.value!r} and accept NONE enforcement."
            )

        # ⚠️ **Network was the field still accepted and ignored.** `mode` was fixed
        # and `network` was not, so the same defect survived one branch over: the
        # policy's default is `DENY`, every caller threads its constraints through
        # (`builtin.run`, `repair.run_checks`), this backend never read it — and the
        # model's own scope section said "Network: deny" while the command it was
        # about to run could reach anything. **A boundary asserted to the model and
        # not applied is worse than no boundary at all**, because the model reasons
        # from it: it will not attempt the forbidden route and will not report one
        # either.
        #
        # The distinction from `mode` is the whole argument for refusing here and
        # accepting `WORKSPACE_WRITE`. `WORKSPACE_WRITE` is *not less* than what this
        # backend does ("runs the command, restricts nothing" is a superset of
        # "may write in the workspace"), so running it is honest. A requested `DENY`
        # that is not applied is not a superset — it is the opposite of what was
        # asked, and the seam states the rule outright: *a backend must refuse a
        # policy it cannot enforce rather than accept it and ignore it.*
        if policy.network is not NetworkAccess.ALLOW:
            raise PolicyNotEnforceable(
                f"this backend provides no containment, so it cannot honour "
                f"network={policy.network.value!r} — a command run here can reach the "
                f"network. Refusing rather than running something other than what was "
                f"asked for. Use a backend that reports `full` (its network namespace "
                f"is real), or grant `network: allow` in the project and the caller's "
                f"grant and accept NONE enforcement."
            )

        def _spawn() -> subprocess.CompletedProcess:
            return subprocess.run(  # noqa: S603 - the whole point of the provider
                argv,
                cwd=str(cwd),
                env=dict(env),
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                shell=False,
                check=False,
            )

        try:
            completed = await asyncio.to_thread(_spawn)
        except subprocess.TimeoutExpired as exc:
            return self._result(
                argv,
                policy,
                started,
                exit_code=KILLED,
                stdout=_text(exc.stdout),
                stderr=_text(exc.stderr),
                timed_out=True,
            )
        except FileNotFoundError:
            # Nothing was spawned, so nothing ran — which `executed=False` says,
            # and which a caller must surface rather than format as a failure of
            # the *command*.
            return self._result(
                argv,
                policy,
                started,
                exit_code=NOT_FOUND,
                stderr=f"command not found: {argv[0]!r}",
                executed=False,
            )
        except OSError as exc:
            return self._result(
                argv,
                policy,
                started,
                exit_code=NOT_FOUND,
                stderr=f"{type(exc).__name__}: {exc}",
                executed=False,
            )

        return self._result(
            argv,
            policy,
            started,
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def _result(
        self,
        argv: tuple[str, ...],
        policy: SandboxExecutionPolicy,
        started: float,
        *,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        executed: bool = True,
    ) -> ExecResult:
        return ExecResult(
            argv=argv,
            exit_code=exit_code,
            stdout=_bound(stdout, self.max_output_bytes),
            stderr=_bound(stderr, self.max_output_bytes),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            # What this execution actually had, not what the policy asked for.
            enforcement=SandboxEnforcement.NONE,
            executed=executed,
        )


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _bound(text: str, limit: int) -> str:
    """Truncate from the middle, keeping both ends.

    A failing test's *first* lines say what ran and its *last* lines say why it
    failed; the middle is the part that repeats. Cutting the tail would hide the
    error, and cutting the head would hide which test it was.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return f"{text[:half]}\n… [{omitted} bytes omitted] …\n{text[-half:]}"
