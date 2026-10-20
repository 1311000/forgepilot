"""The network boundary: enforced, refused — and never merely *asserted*.

``SandboxExecutionPolicy.network`` defaults to ``DENY``, every caller threads its
constraints through it, and the model's own scope section states the same value
(``Network: deny``). For most of this project's life the local backend read
``mode`` and **not** ``network``, so a command ran with an unrestricted network
reach while the model was told the opposite.

⚠️ **A boundary that is asserted to the model and not applied is worse than no
boundary at all.** The model reasons from it: it will not attempt the forbidden
route, and it will not report one either — so the run looks compliant. That is
the same shape as ``ToolDefinition.required_operation`` being consulted by
nothing and ``SandboxExecutionPolicy.mode`` being accepted and ignored, and it is
why this file pins **both** halves, because either can drift on its own:

- **the mechanism** — a backend that cannot deny the network must *refuse* the
  policy rather than accept it (the seam states this outright), and the ``run``
  tool must thread the caller's decision through, or there is nothing to refuse;
- **the claim** — the value in the scope section must be the value the sandbox is
  handed. They are read from one source, so the prompt cannot describe a boundary
  the backend was never asked to apply.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgepilot.config.project import ProjectConfig
from forgepilot.orchestration.context_assembly import assemble_system_sections
from forgepilot.providers.sandbox.local import LocalProcessSandboxPlugin, PolicyNotEnforceable
from forgepilot.runtime.tools.builtin import build_tool_definitions
from forgepilot.seams.refs import PrincipalId, RepoRef, SessionId, TenantId, TurnId
from forgepilot.seams.sandbox import (
    ExecResult,
    SandboxEnforcement,
    SandboxExecutionPolicy,
)
from forgepilot.seams.security import (
    ExecutionConstraints,
    NetworkAccess,
    Operation,
    Principal,
)
from forgepilot.seams.tools import Capabilities, ToolCall, ToolContext

REPO = RepoRef(host="git.corp", namespace="team", name="svc-a")


def _constraints(network: NetworkAccess) -> ExecutionConstraints:
    return ExecutionConstraints.for_principal(
        Principal(principal_id=PrincipalId("alice"), tenant_id=TenantId("acme")),
        allowed_repos=frozenset({REPO}),
        permitted_operations=frozenset({Operation.RUN_TESTS}),
        writable_paths=("src",),
        network=network,
    )


def _run_tool(sandbox):
    return next(d for d in build_tool_definitions(sandbox) if d.name == "run")


def _context(constraints: ExecutionConstraints, workspace_root: Path) -> ToolContext:
    return ToolContext(
        session_id=SessionId("s-network"),
        constraints=constraints,
        turn=TurnId("t-1"),
        step=1,
        # `arm` is not in this path, so the capability is granted directly — what
        # is under test is where the tool *sends* the network decision, not
        # whether it was allowed to run.
        capabilities=Capabilities(exec=True),
        workspace_root=workspace_root,
    )


class _CapturingSandbox:
    """A ``sandbox`` collaborator that only records the policy it was handed.

    The seam is a :class:`~typing.Protocol`, so a collaborator that records is a
    legitimate implementation of it — and this test is about what the *tool*
    decides, so no real backend should be involved in it.
    """

    def __init__(self) -> None:
        self.policies: list[SandboxExecutionPolicy] = []

    def probe(self) -> SandboxEnforcement:
        return SandboxEnforcement.NONE

    async def run(
        self,
        argv,
        *,
        policy: SandboxExecutionPolicy,
        cwd: Path,
        env,
        timeout_s: float,
        stdin: str | None = None,
    ) -> ExecResult:
        self.policies.append(policy)
        return ExecResult(
            argv=tuple(str(a) for a in argv),
            exit_code=0,
            stdout="",
            enforcement=SandboxEnforcement.NONE,
        )


@pytest.mark.parametrize("network", [NetworkAccess.DENY, NetworkAccess.ALLOW])
async def test_the_scope_section_and_the_sandbox_get_the_same_network_decision(
    network: NetworkAccess, tmp_path: Path
) -> None:
    """The anti-drift assertion: one source, so the two cannot disagree.

    If these were two expressions of the same intent, a change to one would leave
    the other describing a boundary that is not there — which is exactly the state
    this test was written in response to.
    """
    constraints = _constraints(network)
    project = ProjectConfig(name="svc-a", repos=(REPO,), repo_prefixes=("git.corp/team/*",))

    scope = next(
        section
        for section in assemble_system_sections(
            project=project, constraints=constraints, workspace_root=tmp_path
        )
        if section.name == "scope"
    )
    claimed = [line for line in scope.render().splitlines() if line.startswith("Network:")]
    assert claimed == [f"Network: {network.value}"], (
        "the model was told something about the network that is not what it was given"
    )

    sandbox = _CapturingSandbox()
    await _run_tool(sandbox).execute(
        ToolCall(call_id="c-1", name="run", arguments_json='{"command": "echo hi"}'),
        _context(constraints, tmp_path),
    )

    assert sandbox.policies, "the run tool never reached the sandbox"
    assert sandbox.policies[-1].network is network, (
        "the prompt and the sandbox were told different things about the network"
    )


async def test_a_network_deny_reaches_the_backend_at_all(tmp_path: Path) -> None:
    """The other half of "refused rather than ignored": there has to be something
    to refuse.

    A tool that dropped the caller's decision — passing its own default, or none —
    would make every backend's refusal unreachable, and the boundary would be
    unenforceable *and* unrefusable at the same time.
    """
    sandbox = _CapturingSandbox()

    await _run_tool(sandbox).execute(
        ToolCall(call_id="c-1", name="run", arguments_json='{"command": "echo hi"}'),
        _context(_constraints(NetworkAccess.DENY), tmp_path),
    )

    assert sandbox.policies[-1].network is NetworkAccess.DENY


async def test_a_deny_is_not_swallowed_by_the_tool_on_a_backend_that_cannot_enforce_it(
    tmp_path: Path,
) -> None:
    """The refusal must leave the tool, because the pipeline is what turns it into
    a result the model reads (``{type}: {message}``, ``is_error=True``).

    Swallowing it here — or turning it into a cheerful ``exit 0`` — would put the
    run back to being silently unenforced, which is the defect this file exists
    for. The message names the network so the model learns the *boundary* was the
    reason rather than the command, the distinction ``runtime/sandbox_denial.py``
    exists to preserve.
    """
    sandbox = LocalProcessSandboxPlugin.__new__(LocalProcessSandboxPlugin)
    sandbox.max_output_bytes = 64_000

    with pytest.raises(PolicyNotEnforceable, match="network"):
        await _run_tool(sandbox).execute(
            ToolCall(call_id="c-1", name="run", arguments_json='{"command": "echo hi"}'),
            _context(_constraints(NetworkAccess.DENY), tmp_path),
        )
