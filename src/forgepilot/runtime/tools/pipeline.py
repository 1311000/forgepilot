"""The seven-phase tool execution pipeline.

::

    tool/call (durable)  ->  pre-execute (pure)  ->  approval  ->  guards
        ->  arm (privileged)  ->  execute  ->  post-execute  ->  finalize
        ->  tool/result (durable)

Three things about the ordering are load-bearing rather than stylistic:

**The call is durable before anything runs.** A crash mid-call leaves a log that
says a tool was invoked, which is what lets recovery close it honestly instead of
the call appearing never to have happened.

**Nothing effectful happens before approval and the guards.** The privileged
phase is ``arm``, and it comes after both. This is the answer to the reference
implementation's defect: there is no hook *before* the human is asked that is
capable of doing anything.

**Exceptions never escape.** A third-party hook that raises becomes an error
result for that one call. One bad listener must not kill a turn, and normalising
here is what makes that true rather than hoped for.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from forgepilot.runtime.credentials import literal_secret
from forgepilot.runtime.guards.registry import GuardRegistry, make_view
from forgepilot.runtime.redaction import Redactor
from forgepilot.runtime.tools.arm import (
    ArmRefused,
    ArmScope,
    literal_secret_refusal,
    mint_capabilities,
)
from forgepilot.runtime.tools.pre_execute import (
    CallPreview,
    PreDecision,
    PreDecisionKind,
    PreExecuteListener,
    PreExecuteScope,
    resolve_decision,
)
from forgepilot.seams.approval import ApprovalOutcome, ApprovalRequest
from forgepilot.seams.credentials import CredentialChannel, CredentialError, placeholders
from forgepilot.seams.refs import TurnId
from forgepilot.seams.sandbox import SandboxEnforcement
from forgepilot.seams.security import ExecutionConstraints, Principal
from forgepilot.seams.session import EventKind
from forgepilot.seams.tools import (
    ResourceTargets,
    ToolCall,
    ToolContext,
    ToolOutcome,
    ToolRegistry,
)

__all__ = ["Phase", "ToolPipeline", "PostExecuteListener"]


class Phase(StrEnum):
    """Phase names, used for the trace and for result metadata."""

    TOOL_CALL = "tool/call"
    PRE_EXECUTE = "pre-execute"
    APPROVAL = "approval"
    GUARDS = "guards"
    ARM = "arm"
    EXECUTE = "execute"
    POST_EXECUTE = "post-execute"
    FINALIZE = "finalize"
    TOOL_RESULT = "tool/result"


#: Rewrites a result on the way out. Runs on a frozen outcome and cannot affect
#: whether the call happened — by this point it already has.
PostExecuteListener = Callable[[ToolCall, ToolOutcome], ToolOutcome | None]


@dataclass
class ToolPipeline:
    """Runs one tool call through every phase."""

    registry: ToolRegistry
    guards: GuardRegistry
    approval: Any
    enforcement: Callable[[], SandboxEnforcement]
    workspace_root: Path | None = None
    #: Who is calling. Required, because a pre-execute listener that cannot say
    #: *who* cannot make a permission decision — and the phase exists to make one.
    #: Handed to the scope as data; see `PreExecuteScope`.
    principal: Principal | None = None
    minimum_enforcement_for_exec: SandboxEnforcement = SandboxEnforcement.FULL
    logger: Any = None
    pre_execute_listeners: list[PreExecuteListener] = field(default_factory=list)
    post_execute_listeners: list[PostExecuteListener] = field(default_factory=list)
    #: Tools this run may use at all. ``None`` means "everything registered".
    #:
    #: Restricting what the model is *shown* is not enough on its own: a model can
    #: name a tool it was never offered, and the registry would happily run it.
    #: The router's selection has to be enforced here, at the point of execution,
    #: or the visible set is merely a suggestion.
    allowed_tools: frozenset[str] | None = None
    #: Phase names in the order the most recent call passed through them.
    #: Exposed for tests and diagnostics; this is how the ordering guarantee is
    #: asserted rather than assumed.
    trace: list[str] = field(default_factory=list)
    #: Where ``${NAME}`` is resolved. **Optional, and its absence is a refusal,
    #: not a bypass**: a call that asks for a credential with no channel configured
    #: fails in ``arm`` rather than running the command with the placeholder left
    #: in it (which would fail somewhere else, looking like something else).
    credentials: CredentialChannel | None = None
    #: The gate ``arm`` uses to recognise a literal secret. ``None`` means the
    #: process default — see `runtime.redaction.default_redactor`.
    redactor: Redactor | None = None

    def on_pre_execute(self, listener: PreExecuteListener) -> None:
        self.pre_execute_listeners.append(listener)

    def on_post_execute(self, listener: PostExecuteListener) -> None:
        self.post_execute_listeners.append(listener)

    # -- entry point -------------------------------------------------------
    async def invoke(
        self,
        call: ToolCall,
        *,
        session: Any,
        turn: TurnId,
        step: int,
        constraints: ExecutionConstraints,
        workspace_root: Path | None = None,
    ) -> ToolOutcome:
        """Run ``call``. Always returns an outcome; never raises."""
        self.trace = []
        root = workspace_root if workspace_root is not None else self.workspace_root

        definition = self.registry.get(call.name)
        if definition is None:
            outcome = ToolOutcome(
                content=f"no such tool: {call.name!r}", is_error=True, meta={"phase": "dispatch"}
            )
            await self._record_call(session, call, turn, step)
            await self._record_result(session, call, outcome, turn, step)
            return outcome

        if self.allowed_tools is not None and call.name not in self.allowed_tools:
            # Not in this task's selected set. Recorded as a normal refusal so the
            # model sees it and stops asking, rather than being silently ignored.
            outcome = ToolOutcome(
                content=(
                    f"tool {call.name!r} is not part of this task's tool set. Available: "
                    f"{sorted(self.allowed_tools)}"
                ),
                is_error=True,
                meta={"phase": "dispatch", "not_selected": True},
            )
            await self._record_call(session, call, turn, step)
            await self._record_result(session, call, outcome, turn, step)
            return outcome

        parse_error: str | None = None
        try:
            arguments = json.loads(call.arguments_json) if call.arguments_json.strip() else {}
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            # Kept as a normal error result rather than a crash: the raw string is
            # already in the log, so the failure stays replayable.
            arguments, parse_error = {}, f"could not parse arguments: {exc}"

        self.trace.append(Phase.TOOL_CALL)
        await self._record_call(session, call, turn, step)
        # Buffering the call is not enough: "durable before execution" means the
        # log entry survives a crash *while the tool is running*. Without this
        # flush a crash mid-call would lose the record of the call entirely, and
        # recovery could not tell "never started" from "started and died".
        await session.flush()

        targets = (
            definition.describe_targets(arguments)
            if definition.describe_targets and not parse_error
            else ResourceTargets()
        )
        preview = CallPreview(
            tool=definition.name,
            effects=definition.effects,
            arguments=arguments,
            arguments_json=call.arguments_json,
            targets=targets,
            workspace_root=root,
            required_operation=definition.required_operation,
        )

        # Guards are captured now, at call time. A plugin registering a guard
        # while this call is in flight does not affect it, and cannot remove one
        # to get out from under it either.
        snapshot = self.guards.snapshot()

        if parse_error is not None:
            return await self._finish(
                session, call, turn, step,
                ToolOutcome(content=parse_error, is_error=True, meta={"phase": "parse"}),
            )

        # --- phase: pre-execute (pure) -------------------------------------
        self.trace.append(Phase.PRE_EXECUTE)
        if self.principal is None:
            # Refused rather than defaulted. A listener that checks permission
            # without knowing the caller would have to skip the check, and
            # "skipped because we did not know who it was" is the wrong way round
            # for the one phase whose entire job is to decide.
            return await self._finish(
                session, call, turn, step,
                self._refusal(
                    Phase.PRE_EXECUTE,
                    "no principal is attached to this pipeline, so no pre-execute "
                    "listener can decide anything about it",
                ),
            )
        scope = PreExecuteScope(
            logger=self.logger, constraints=constraints, principal=self.principal
        )
        try:
            decision = resolve_decision(tuple(self.pre_execute_listeners), preview, scope)
        except Exception as exc:  # noqa: BLE001 - a bad hook must not kill the turn
            return await self._finish(
                session, call, turn, step,
                ToolOutcome(
                    content=f"a pre-execute hook failed: {type(exc).__name__}: {exc}",
                    is_error=True,
                    meta={"phase": Phase.PRE_EXECUTE},
                ),
            )

        if decision.kind is PreDecisionKind.DENY:
            return await self._finish(
                session, call, turn, step,
                self._refusal(Phase.PRE_EXECUTE, decision.reason or "denied"),
            )

        # --- phase: approval ------------------------------------------------
        if decision.kind is PreDecisionKind.ASK:
            self.trace.append(Phase.APPROVAL)
            outcome = await self._ask(session, call, turn, step, decision)
            if outcome is not None:
                return await self._finish(session, call, turn, step, outcome)

        # --- phase: guards (monotonic) --------------------------------------
        self.trace.append(Phase.GUARDS)
        verdict = snapshot.evaluate(
            make_view(
                tool=definition.name,
                effects=frozenset(str(e) for e in definition.effects),
                targets=targets,
                arguments=arguments,
                constraints=constraints,
                required_operation=definition.required_operation,
            )
        )
        if verdict.decision.value == "deny":
            return await self._finish(
                session, call, turn, step,
                self._refusal(Phase.GUARDS, verdict.reason, guard=verdict.guard),
            )

        # --- phase: arm (the only privileged one) ---------------------------
        self.trace.append(Phase.ARM)
        try:
            # Scanned **before** capabilities are minted. Both are refusals, and the
            # literal is the more specific one: a model told "the sandbox is too
            # weak here" for a command it must rewrite anyway learns the wrong
            # lesson from the wrong sentence.
            await self._claim_credentials(session, targets, turn, step)
            capabilities = mint_capabilities(
                effects=definition.effects,
                scope=ArmScope(
                    session_id=session.header.session_id,
                    constraints=constraints,
                    enforcement=self.enforcement(),
                    workspace_root=root,
                ),
                minimum_enforcement_for_exec=self.minimum_enforcement_for_exec,
            )
        except ArmRefused as exc:
            return await self._finish(
                session, call, turn, step,
                self._refusal(Phase.ARM, str(exc)),
            )

        # --- phase: execute -------------------------------------------------
        self.trace.append(Phase.EXECUTE)
        context = ToolContext(
            session_id=session.header.session_id,
            constraints=constraints,
            turn=turn,
            step=step,
            capabilities=capabilities,
            workspace_root=root,
            credentials=self.credentials,
        )
        try:
            outcome = await definition.execute(call, context)
        except Exception as exc:  # noqa: BLE001
            outcome = ToolOutcome(
                content=f"{type(exc).__name__}: {exc}",
                is_error=True,
                meta={"phase": Phase.EXECUTE},
            )

        # --- phase: post-execute --------------------------------------------
        self.trace.append(Phase.POST_EXECUTE)
        for listener in self.post_execute_listeners:
            try:
                replacement = listener(call, outcome)
            except Exception as exc:  # noqa: BLE001
                replacement = ToolOutcome(
                    content=f"a post-execute hook failed: {type(exc).__name__}: {exc}",
                    is_error=True,
                    meta={"phase": Phase.POST_EXECUTE},
                )
            if replacement is not None:
                outcome = replacement

        # --- phase: finalize (content-only, last word) ----------------------
        self.trace.append(Phase.FINALIZE)
        if definition.finalize_content is not None:
            try:
                outcome = ToolOutcome(
                    content=definition.finalize_content(outcome),
                    is_error=outcome.is_error,
                    meta=outcome.meta,
                )
            except Exception as exc:  # noqa: BLE001
                outcome = ToolOutcome(
                    content=f"finalize_content failed: {type(exc).__name__}: {exc}",
                    is_error=True,
                    meta={"phase": Phase.FINALIZE},
                )

        return await self._finish(session, call, turn, step, outcome)

    # -- helpers -----------------------------------------------------------
    def _refusal(self, phase: Phase, reason: str, **extra: Any) -> ToolOutcome:
        return ToolOutcome(
            content=reason,
            is_error=True,
            meta={"phase": str(phase), "refused": True, **extra},
        )

    async def _claim_credentials(
        self, session: Any, targets: ResourceTargets, turn: TurnId, step: int
    ) -> tuple[str, ...]:
        """Refuse a secret-shaped literal; check the names a command will need.

        **This is what makes the credential channel a mechanism.** Everything else
        about it is a convention a model can ignore by typing the value where it
        belongs; here the value is recognised and the call is refused.

        Two deliberate choices:

        - **A literal is refused, not redacted.** Redacting it would run the
          command with a placeholder in it and fail somewhere else, while the
          model went on believing the credential was in play — a wrong diagnosis
          is worse than no run.
        - **Names are validated here, substituted later.** The substitution needs
          the final command text, which belongs to the tool that builds it; this
          phase's job is to be the last word on whether the call proceeds at all.
          When there is no channel, or the name is not declared, that is a refusal
          too: the alternative is a shell line with an empty secret in it.

        The audit event carries the names and never a value.
        """
        names: dict[str, None] = {}
        for command in targets.commands:
            text = str(command)
            mark = literal_secret(text, self.redactor)
            if mark is not None:
                raise literal_secret_refusal(mark)
            for name in placeholders(text):
                names.setdefault(name, None)
        for name in names:
            await self._resolve_credential(name)
        if names:
            await session.append(
                EventKind.CREDENTIAL_CLAIMED,
                {"names": sorted(names)},
                turn=turn,
                step=step,
            )
        return tuple(names)

    async def _resolve_credential(self, name: str) -> None:
        """Confirm the name resolves, and turn a failure into an arm refusal."""
        if self.credentials is None:
            raise ArmRefused(
                f"this command uses ${{{name}}} but no credential channel is configured "
                f"for this session, so there is nothing to substitute. Ask whoever runs "
                f"the deployment to declare the name, or run the command without the "
                f"secret."
            )
        try:
            await self.credentials.resolve(name)
        except CredentialError as exc:
            raise ArmRefused(str(exc)) from exc

    async def _ask(
        self,
        session: Any,
        call: ToolCall,
        turn: TurnId,
        step: int,
        decision: PreDecision,
    ) -> ToolOutcome | None:
        """Ask a human. Returns a refusal outcome, or ``None`` to proceed.

        ``allowed-once`` is the only permissive outcome; everything else —
        including "nobody answered" — refuses. Audit events are log-only, so the
        model is never told that a call was proposed and declined.
        """
        request = ApprovalRequest(
            request_id=f"{call.call_id}-approval",
            session_id=session.header.session_id,
            tool_name=call.name,
            arguments_json=call.arguments_json,
            reason=decision.reason,
        )
        await session.append(
            EventKind.APPROVAL_ASKED,
            {"callId": call.call_id, "tool": call.name, "reason": decision.reason},
            turn=turn,
            step=step,
        )
        # Same rule as `tool/call`, and for a sharper reason: the prompt is how a
        # human learns there is something to answer, and the answer arrives on a
        # *different* channel that reads the log. Buffered, the prompt is
        # invisible, nobody answers, and the turn hangs with no error — the
        # failure mode is a deadlock, not a stack trace.
        await session.flush()
        try:
            outcome = await self.approval.request(request)
        except Exception:  # noqa: BLE001 - an answerer that breaks is not a yes
            outcome = ApprovalOutcome.UNAVAILABLE

        await session.append(
            EventKind.APPROVAL_DECIDED,
            {"callId": call.call_id, "outcome": str(outcome)},
            turn=turn,
            step=step,
        )

        if outcome is ApprovalOutcome.ALLOWED_ONCE:
            return None
        return self._refusal(
            Phase.APPROVAL,
            f"approval {outcome.value}",
            outcome=str(outcome),
        )

    async def _record_call(self, session: Any, call: ToolCall, turn: TurnId, step: int) -> None:
        await session.tool_call(
            turn, step, call_id=call.call_id, name=call.name, arguments_json=call.arguments_json
        )

    async def _record_result(
        self, session: Any, call: ToolCall, outcome: ToolOutcome, turn: TurnId, step: int
    ) -> None:
        await session.tool_result(
            turn,
            step,
            call_id=call.call_id,
            content=outcome.content,
            is_error=outcome.is_error,
            meta=outcome.meta,
        )

    async def _finish(
        self,
        session: Any,
        call: ToolCall,
        turn: TurnId,
        step: int,
        outcome: ToolOutcome,
    ) -> ToolOutcome:
        self.trace.append(Phase.TOOL_RESULT)
        await self._record_result(session, call, outcome, turn, step)
        return outcome
