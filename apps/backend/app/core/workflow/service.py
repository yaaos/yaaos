"""`WorkflowEngine` + the three core/tasks task bodies.

Provides:
- `start_step` body — branches on command isinstance. AgentDispatchCommands call
  `command.dispatch(inputs, ctx, session=s)` to enqueue an AgentCommand row and
  park in `awaiting_agent`; HITLCommands run execute() and park in `awaiting_human`;
  LocalCommands run execute() and advance to route_workflow immediately.
- `handle_agent_event` body — validates the event matches the pending
  command id, clears it, enqueues `route_workflow`. Idempotent: stale
  events exit cleanly.
- `route_workflow` body — persists outcome, applies retry budget, evaluates
  the step's transitions map, enqueues the next `start_step` or marks the
  workflow terminal. Atomic state-change + outbox enqueue in one transaction.
- `request_cancel` + `resume_hitl` admin APIs.

The three-task split keeps workers free during long-running AgentCommands.
See `apps/backend/docs/core_workflow.md`.
"""

from __future__ import annotations

from datetime import UTC as _UTC
from datetime import datetime
from typing import Any, ClassVar, Protocol, cast
from uuid import UUID

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import command_id_var, workflow_execution_id_var
from app.core.database import session as db_session
from app.core.observability import current_traceparent, with_remote_parent_span
from app.core.sse import GeneralEventKind, publish_general_after_commit
from app.core.tasks import TaskRef, enqueue, task
from app.core.workflow.models import PendingHumanDecisionRow, WorkflowExecutionRow
from app.core.workflow.types import (
    TERMINAL_STATES,
    AgentDispatchCommand,
    CommandContext,
    CommandNotRegisteredError,
    Empty,
    HasAgentResponseHandler,
    HITLCommand,
    NullDispatch,
    Outcome,
    OutcomeKind,
    RetryPolicy,
    StepRef,
    TerminalAction,
    Workflow,
    WorkflowCommand,
    WorkflowError,
    WorkflowExecutionNotFoundError,
    WorkflowNotFoundError,
    WorkflowState,
    WorkflowValidationError,
    _step_outputs_var,
)

log = structlog.get_logger("core.workflow")
_tracer = trace.get_tracer("core.workflow")

# States where no natural task tick will arrive soon, so `request_cancel`
# proactively enqueues `route_workflow`. AWAITING_AGENT is excluded — the
# agent's terminal event is the trigger there.
_PROACTIVE_CANCEL_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.PENDING, WorkflowState.RUNNING, WorkflowState.AWAITING_HUMAN}
)


# ── Typed helpers for command-class attribute access ────────────────────
# Used in `register_workflow` to read `kind` and `recovers_failure_label`
# from command classes without untyped getattr calls.


class _CommandClassProto(Protocol):
    kind: ClassVar[str]


class _RecoveryCommandClassProto(Protocol):
    kind: ClassVar[str]
    recovers_failure_label: ClassVar[str]


class WorkflowFailedPayload(BaseModel):
    """Audit payload for `workflow.failed` rows written by the engine on
    terminal-fail. Generic — owned by `core/workflow`, not domain-specific."""

    workflow_execution_id: str
    ticket_id: str
    failed_step_id: str | None
    failure_reason: str | None


class WorkflowCancelledPayload(BaseModel):
    """Audit payload for `workflow.cancelled` rows written by the engine on
    terminal-cancel. Generic — owned by `core/workflow`, not domain-specific."""

    workflow_execution_id: str
    ticket_id: str
    cancelled_step_id: str | None


# ── The three taskiq task bodies ────────────────────────────────────────


@task("workflow.start_step", queue="workflow", max_retries=1)
async def start_step(
    *,
    workflow_execution_id: str,
    step_id: str,
    attempt: int,
    inputs: dict[str, Any],
    traceparent: str | None = None,
) -> None:
    """Dispatch the step. Branches on the WorkflowCommand type:

    - **AgentDispatchCommand** (`isinstance(cmd, AgentDispatchCommand)`) — calls
      `command.dispatch(typed_inputs, ctx, session=s)` to enqueue an
      AgentCommand row, parks the workflow in `awaiting_agent`, and sets
      `pending_agent_command_id` to the returned `command_id`.
    - **Local** — runs the command inline, persists its outcome, enqueues
      `route_workflow` via the outbox in the same transaction.
    - **HITL** — runs the command (which must return `Outcome.hitl_pending`),
      writes the `pending_human_decisions` row, sets `state = awaiting_human`.

    Span: taskiq's auto-instrumented `task:workflow.start_step` span is the
    task boundary.  The inner `workflow.command.<Kind>` span (opened by
    `_safe_execute` for Local/HITL and by `_start_step_impl` for Workspace)
    is a direct child of the taskiq task span.
    """
    wf_token = workflow_execution_id_var.set(workflow_execution_id)
    try:
        await _start_step_impl(
            workflow_execution_id=workflow_execution_id,
            step_id=step_id,
            attempt=attempt,
            inputs=inputs,
        )
    finally:
        workflow_execution_id_var.reset(wf_token)


async def _start_step_impl(
    *,
    workflow_execution_id: str,
    step_id: str,
    attempt: int,
    inputs: dict[str, Any],
) -> None:
    async with db_session() as s:
        wfx = await _load_execution(s, workflow_execution_id)
        if wfx is None:
            log.warning("workflow.start_step.unknown_execution", workflow_execution_id=workflow_execution_id)
            return

        # Cancellation check — route through the finalizer (one-shot) before
        # entering CANCELLED, unless the finalizer is already in flight.
        if wfx.cancel_requested:
            _ss_engine = get_engine()
            try:
                _ss_wf = _ss_engine.get_workflow(wfx.workflow_name, version=wfx.workflow_version)
            except WorkflowNotFoundError:
                _ss_wf = None

            # If this IS the finalizer step (cancel pathway dispatched it already),
            # let it run — the cancel flag is the post-finalizer discriminator.
            if _ss_wf is not None and _ss_wf.finalizer is not None and step_id == _ss_wf.finalizer.step_id:
                pass  # fall through to normal dispatch
            elif not wfx.finalizer_fired and _ss_wf is not None and _ss_wf.finalizer is not None:
                # Cancel detected before the finalizer was fired — route through it.
                wfx.finalizer_fired = True
                wfx.state = WorkflowState.RUNNING.value
                _publish_state_changed(s, wfx)
                log.debug(
                    "workflow.start_step.cancel_routes_to_finalizer",
                    workflow_execution_id=workflow_execution_id,
                    finalizer_step_id=_ss_wf.finalizer.step_id,
                )
                await _enqueue_start_step(s, wfx, _ss_wf, _ss_wf.finalizer.step_id, attempt=0)
                await s.commit()
                return
            else:
                # No finalizer, or finalizer already dispatched for a different step.
                log.debug(
                    "workflow.start_step.cancelled_pre_dispatch",
                    workflow_execution_id=workflow_execution_id,
                )
                await _enter_cancelled_state(s, wfx, wfx.current_step_id)
                await s.commit()
                return

        # State guard. start_step is only valid while running.
        if wfx.state != WorkflowState.RUNNING.value:
            log.debug(
                "workflow.start_step.skip_not_running",
                workflow_execution_id=workflow_execution_id,
                state=wfx.state,
            )
            return

        engine = get_engine()
        wf = engine.get_workflow(wfx.workflow_name, version=wfx.workflow_version)
        step = _resolve_step(wfx, wf, step_id)
        if step is None:
            log.error(
                "workflow.start_step.unknown_step",
                workflow_execution_id=workflow_execution_id,
                step_id=step_id,
            )
            await _enter_terminal_state(s, wfx, WorkflowState.FAILED)
            await s.commit()
            return

        command_kind = step.command_class.kind
        try:
            command = engine.get_command(command_kind)
        except CommandNotRegisteredError:
            log.error(
                "workflow.start_step.command_not_registered",
                workflow_execution_id=workflow_execution_id,
                command_kind=command_kind,
            )
            await _enter_terminal_state(s, wfx, WorkflowState.FAILED)
            await s.commit()
            return

        # Reconstruct typed inputs from the serialised dict stored in the task args.
        try:
            typed_inputs: BaseModel = command.Inputs.model_validate(inputs)  # type: ignore[attr-defined]
        except Exception as exc:
            log.error(
                "workflow.start_step.inputs_validation_failed",
                workflow_execution_id=workflow_execution_id,
                step_id=step_id,
                error=str(exc),
            )
            await _enter_terminal_state(s, wfx, WorkflowState.FAILED)
            await s.commit()
            return

        # cmd_ctx traceparent is the currently-active span's traceparent —
        # the taskiq auto-span for this task body.  Workspace commands rebuild
        # cmd_ctx inside the command span so the agent receives that span's id.
        cmd_ctx = CommandContext(
            workflow_execution_id=str(wfx.id),
            ticket_id=str(wfx.ticket_id),
            step_id=step_id,
            attempt=attempt,
            traceparent=current_traceparent(),
        )

        if isinstance(command, AgentDispatchCommand):
            # Agent-dispatch commands enqueue an AgentCommand durably inside this
            # transaction via the command's own `dispatch` method. The engine
            # parks the execution in `awaiting_agent` and stores the returned
            # `command_id` as `pending_agent_command_id`. The terminal
            # AgentEvent arrives via `handle_agent_event` to resume routing.
            # `NullDispatch` is the signal from WorkspaceOpCommand.dispatch when
            # build_command returns None (e.g. CleanupWorkspace with no workspace_id)
            # — treat as a successful local outcome to skip the park.
            wfx.current_step_id = step_id
            _stamp_step_started(wfx, step_id)
            outer_span = trace.get_current_span()
            kind = command_kind
            with with_remote_parent_span(
                _tracer, f"workflow.command.{kind}", wfx.otel_trace_context
            ) as cmd_span:
                cmd_span.set_attribute("command.kind", kind)
                cmd_span.set_attribute("command.category", "agent_dispatch")
                cmd_span.set_attribute("workflow.step_id", step_id)
                cmd_span.set_attribute("workflow.attempt", attempt)
                # Rebuild CommandContext with the command span's own traceparent so
                # downstream dispatch (→ enqueue_command → agent) parents correctly.
                ws_cmd_ctx = CommandContext(
                    workflow_execution_id=cmd_ctx.workflow_execution_id,
                    ticket_id=cmd_ctx.ticket_id,
                    step_id=cmd_ctx.step_id,
                    attempt=cmd_ctx.attempt,
                    traceparent=current_traceparent(),
                )
                null_dispatch = False
                try:
                    command_id = await command.dispatch(typed_inputs, ws_cmd_ctx, session=s)
                except NullDispatch:
                    null_dispatch = True
                    command_id = None
                except Exception as exc:
                    cmd_span.record_exception(exc)
                    cmd_span.set_status(StatusCode.ERROR, str(exc))
                    outer_span.set_status(StatusCode.ERROR, str(exc))
                    log.exception(
                        "workflow.command.agent_dispatch_raised",
                        workflow_execution_id=workflow_execution_id,
                        step_id=step_id,
                    )
                    await _enter_terminal_state(s, wfx, WorkflowState.FAILED)
                    await s.commit()
                    return

            if null_dispatch:
                # build_command returned None — treat as a successful local step.
                null_outcome = Outcome.success()
                wfx.step_attempts = {**wfx.step_attempts, step_id: attempt}
                await enqueue(
                    ROUTE_WORKFLOW,
                    args={
                        "workflow_execution_id": workflow_execution_id,
                        "completed_step_id": step_id,
                        "outcome_label": null_outcome.label,
                        "outputs": _outcome_payload(null_outcome),
                        "retryable": null_outcome.retryable,
                        "traceparent": wfx.otel_trace_context,
                    },
                    session=s,
                )
                await s.commit()
                return

            wfx.pending_agent_command_id = command_id
            wfx.state = WorkflowState.AWAITING_AGENT.value
            _publish_state_changed(s, wfx)
            log.debug(
                "workflow.start_step.agent_dispatched",
                workflow_execution_id=workflow_execution_id,
                command_kind=command_kind,
                command_id=str(command_id),
            )
            await s.commit()
            return

        is_hitl = isinstance(command, HITLCommand)

        if is_hitl:
            # HITL: stamp the step and execute inline; no SAVEPOINT needed since
            # HITLCommand has no DB writes and must return hitl_pending.
            wfx.current_step_id = step_id
            _stamp_step_started(wfx, step_id)
            outcome = await _safe_execute(
                command,
                typed_inputs,
                cmd_ctx,
                traceparent=wfx.otel_trace_context,
                category="hitl",
                session=None,
            )
            if outcome.kind is not OutcomeKind.HITL_PENDING:
                log.error(
                    "workflow.start_step.hitl_command_returned_non_pending",
                    workflow_execution_id=workflow_execution_id,
                    outcome_kind=outcome.kind.value,
                )
                await _enter_terminal_state(s, wfx, WorkflowState.FAILED)
                await s.commit()
                return
            s.add(
                PendingHumanDecisionRow(
                    workflow_execution_id=wfx.id,
                    question_payload=dict(
                        outcome.hitl_question.model_dump() if outcome.hitl_question else {}
                    ),
                )
            )
            wfx.state = WorkflowState.AWAITING_HUMAN.value
            _publish_state_changed(s, wfx)
            await s.commit()
            return

        # LocalCommand: execute inside a SAVEPOINT so command writes,
        # step_attempts, and the outbox enqueue are all-or-nothing.
        # If the command raises, _safe_execute re-raises; the savepoint rolls
        # back every write; the outer session then records a terminal FAILED
        # state without enqueuing route_workflow.
        local_exc: Exception | None = None
        try:
            async with s.begin_nested():
                wfx.current_step_id = step_id
                _stamp_step_started(wfx, step_id)
                outcome = await _safe_execute(
                    command,
                    typed_inputs,
                    cmd_ctx,
                    traceparent=wfx.otel_trace_context,
                    category="local",
                    session=s,
                )
                wfx.step_attempts = {**wfx.step_attempts, step_id: attempt}
                await enqueue(
                    ROUTE_WORKFLOW,
                    args={
                        "workflow_execution_id": workflow_execution_id,
                        "completed_step_id": step_id,
                        "outcome_label": outcome.label,
                        "outputs": _outcome_payload(outcome),
                        "retryable": outcome.retryable,
                        "traceparent": wfx.otel_trace_context,
                    },
                    session=s,
                )
        except Exception as exc:
            local_exc = exc

        if local_exc is not None:
            # Savepoint rolled back: command writes + step_attempts + outbox all
            # undone. Refresh wfx (expired by savepoint rollback) and enter FAILED.
            await s.refresh(wfx)
            log.warning(
                "workflow.start_step.local_command_raised",
                workflow_execution_id=workflow_execution_id,
                step_id=step_id,
                error=str(local_exc),
            )
            await _enter_terminal_state(s, wfx, WorkflowState.FAILED)
            await s.commit()
            return

        await s.commit()


@task("workflow.handle_agent_event", queue="workflow", max_retries=1)
async def handle_agent_event(
    *,
    workflow_execution_id: str,
    agent_command_id: str,
    outcome_label: str,
    outputs: dict[str, Any],
    traceparent: str | None = None,
) -> None:
    """Triggered when an AgentCommand's terminal event arrives at
    `core/agent_gateway`. Validates against `pending_agent_command_id`
    (race guard), clears it, transitions back to `running`, enqueues
    `route_workflow`. Idempotent: a duplicate event for a workflow that
    has already advanced exits cleanly.
    """
    wf_token = workflow_execution_id_var.set(workflow_execution_id)
    cmd_token = command_id_var.set(agent_command_id)
    try:
        with with_remote_parent_span(_tracer, "workflow.handle_agent_event", traceparent) as span:
            span.set_attribute("workflow.outcome_label", outcome_label)
            await _handle_agent_event_impl(
                workflow_execution_id=workflow_execution_id,
                agent_command_id=agent_command_id,
                outcome_label=outcome_label,
                outputs=outputs,
                traceparent=traceparent,
            )
    finally:
        command_id_var.reset(cmd_token)
        workflow_execution_id_var.reset(wf_token)


async def _handle_agent_event_impl(
    *,
    workflow_execution_id: str,
    agent_command_id: str,
    outcome_label: str,
    outputs: dict[str, Any],
    traceparent: str | None,
) -> None:
    async with db_session() as s:
        wfx = await _load_execution(s, workflow_execution_id)
        if wfx is None:
            log.warning(
                "workflow.handle_agent_event.unknown_execution",
                workflow_execution_id=workflow_execution_id,
            )
            return

        if wfx.state != WorkflowState.AWAITING_AGENT.value:
            log.debug(
                "workflow.handle_agent_event.skip_state",
                workflow_execution_id=workflow_execution_id,
                state=wfx.state,
            )
            return

        if wfx.pending_agent_command_id is None or str(wfx.pending_agent_command_id) != agent_command_id:
            log.debug(
                "workflow.handle_agent_event.stale_command_id",
                workflow_execution_id=workflow_execution_id,
                expected=str(wfx.pending_agent_command_id),
                received=agent_command_id,
            )
            return

        completed_step_id = wfx.current_step_id
        wfx.pending_agent_command_id = None
        wfx.state = WorkflowState.RUNNING.value
        _publish_state_changed(s, wfx)

        # For CodingAgentCommand steps on success, call handle_response to
        # validate the agent's JSON output and produce a typed Outcome.
        # `HasAgentResponseHandler` is a @runtime_checkable Protocol in types.py
        # so isinstance is safe — avoids an import cycle (core.coding_agent →
        # core.workflow but NOT the reverse).
        retryable = True
        if outcome_label == "success" and completed_step_id is not None:
            engine = get_engine()
            wf = engine.get_workflow(wfx.workflow_name, version=wfx.workflow_version)
            step = _resolve_step(wfx, wf, completed_step_id)
            if step is not None:
                try:
                    command = engine.get_command(step.command_class.kind)
                except CommandNotRegisteredError:
                    command = None
                if isinstance(command, HasAgentResponseHandler):
                    cmd_ctx = CommandContext(
                        workflow_execution_id=str(wfx.id),
                        ticket_id=str(wfx.ticket_id),
                        step_id=completed_step_id,
                        attempt=_get_attempt(wfx, completed_step_id),
                        traceparent=traceparent,
                    )
                    handle_outcome = await command.handle_response(outputs.get("output", ""), cmd_ctx)
                    outcome_label = handle_outcome.label
                    outputs = _outcome_payload(handle_outcome)
                    retryable = handle_outcome.retryable

        await enqueue(
            ROUTE_WORKFLOW,
            args={
                "workflow_execution_id": workflow_execution_id,
                "completed_step_id": completed_step_id,
                "outcome_label": outcome_label,
                "outputs": outputs,
                "retryable": retryable,
                "traceparent": wfx.otel_trace_context,
            },
            session=s,
        )
        await s.commit()


@task("workflow.route_workflow", queue="workflow", max_retries=1)
async def route_workflow(
    *,
    workflow_execution_id: str,
    completed_step_id: str | None,
    outcome_label: str | None,
    outputs: dict[str, Any],
    retryable: bool = True,
    traceparent: str | None = None,
) -> None:
    """Persist the completed step's outcome, apply retry budget, evaluate
    the step's transitions, and either enqueue the next `start_step` or
    mark the workflow terminal. Atomic transition + outbox enqueue in a
    single transaction.
    """
    wf_token = workflow_execution_id_var.set(workflow_execution_id)
    try:
        _task_span = trace.get_current_span()
        if completed_step_id is not None:
            _task_span.set_attribute("workflow.completed_step_id", completed_step_id)
        if outcome_label is not None:
            _task_span.set_attribute("workflow.outcome_label", outcome_label)
        await _route_workflow_impl(
            workflow_execution_id=workflow_execution_id,
            completed_step_id=completed_step_id,
            outcome_label=outcome_label,
            outputs=outputs,
            retryable=retryable,
            traceparent=traceparent,
        )
    finally:
        workflow_execution_id_var.reset(wf_token)


async def _route_workflow_impl(
    *,
    workflow_execution_id: str,
    completed_step_id: str | None,
    outcome_label: str | None,
    outputs: dict[str, Any],
    retryable: bool = True,
    traceparent: str | None,
) -> None:
    async with db_session() as s:
        wfx = await _load_execution(s, workflow_execution_id)
        if wfx is None:
            log.warning(
                "workflow.route_workflow.unknown_execution",
                workflow_execution_id=workflow_execution_id,
            )
            return

        if WorkflowState(wfx.state) in TERMINAL_STATES:
            log.debug(
                "workflow.route_workflow.skip_terminal",
                workflow_execution_id=workflow_execution_id,
                state=wfx.state,
            )
            return

        # Early cancel check — fires only when the finalizer has NOT yet been
        # dispatched. Post-finalizer discrimination happens in the COMPLETE_WORKFLOW
        # and FAIL_WORKFLOW target branches further below (gated on
        # `finalizer_fired AND completed_step_id == wf.finalizer.step_id`).
        if wfx.cancel_requested and not wfx.finalizer_fired:
            _rw_engine = get_engine()
            try:
                _rw_wf = _rw_engine.get_workflow(wfx.workflow_name, version=wfx.workflow_version)
            except WorkflowNotFoundError:
                _rw_wf = None

            if _rw_wf is not None and _rw_wf.finalizer is not None:
                # Route through finalizer first (one-shot).
                wfx.finalizer_fired = True
                wfx.state = WorkflowState.RUNNING.value
                _publish_state_changed(s, wfx)
                log.debug(
                    "workflow.route_workflow.cancel_routes_to_finalizer",
                    workflow_execution_id=workflow_execution_id,
                    finalizer_step_id=_rw_wf.finalizer.step_id,
                )
                await _enqueue_start_step(s, wfx, _rw_wf, _rw_wf.finalizer.step_id, attempt=0)
                await s.commit()
                return

            # No finalizer: enter CANCELLED directly.
            log.debug(
                "workflow.route_workflow.cancelled_at_route",
                workflow_execution_id=workflow_execution_id,
            )
            await _enter_cancelled_state(s, wfx, wfx.current_step_id)
            await s.commit()
            return

        engine = get_engine()
        wf = engine.get_workflow(wfx.workflow_name, version=wfx.workflow_version)

        # Initial call from start(): no completed step. Bootstrap by
        # enqueueing the entry step.
        # Guard: a proactive cancel enqueue arrives with completed_step_id=None
        # while the finalizer is already in flight (finalizer_fired=TRUE). Discard
        # this bootstrap call; the natural finalizer-completion route_workflow will
        # discriminate via cancel_requested.
        if completed_step_id is None and wfx.cancel_requested:
            log.debug(
                "workflow.route_workflow.discard_bootstrap_on_cancel",
                workflow_execution_id=workflow_execution_id,
            )
            return

        if completed_step_id is None:
            wfx.state = WorkflowState.RUNNING.value
            _publish_state_changed(s, wfx)
            org_id = _workflow_org_id(wfx)
            if wf.on_start is not None:
                with with_remote_parent_span(
                    _tracer, "workflow.callback.on_start", wfx.otel_trace_context
                ) as cb_span:
                    try:
                        await wf.on_start(
                            workflow_execution_id=wfx.id,
                            workflow_name=wfx.workflow_name,
                            ticket_id=wfx.ticket_id,
                            org_id=org_id,
                            session=s,
                        )
                    except Exception as exc:
                        cb_span.record_exception(exc)
                        cb_span.set_status(StatusCode.ERROR, str(exc))
                        raise
            await _enqueue_start_step(s, wfx, wf, wf.entry.step_id, attempt=0)
            await s.commit()
            return

        # Persist outcome + outputs to step_state[completed_step_id].
        _persist_outputs(wfx, completed_step_id, outcome_label, outputs)

        step = _resolve_step(wfx, wf, completed_step_id)
        if step is None:
            log.error(
                "workflow.route_workflow.unknown_completed_step",
                workflow_execution_id=workflow_execution_id,
                step_id=completed_step_id,
            )
            await _enter_terminal_state(s, wfx, WorkflowState.FAILED)
            await s.commit()
            return

        # Tier-1 recovery: if the failure label maps to a recovery command on
        # this workflow (e.g. `auth_expired → RefreshWorkspaceAuth`), insert the
        # recovery command as a synthetic step that runs BEFORE the original
        # step retries. Recovery fires at most once per step instance.
        if outcome_label and outcome_label != "success":
            recovery_class = engine._get_recovery_class(wf.name, wf.version, outcome_label)
            if recovery_class is not None and not _has_recovered(wfx, completed_step_id):
                _mark_recovered(wfx, completed_step_id, outcome_label)
                wfx.step_attempts = {**wfx.step_attempts, completed_step_id: 0}
                recovery_step_id = f"_recover_{completed_step_id}"
                wfx.state = WorkflowState.RUNNING.value
                _publish_state_changed(s, wfx)
                log.debug(
                    "workflow.route_workflow.recovery_inserted",
                    workflow_execution_id=workflow_execution_id,
                    failed_step_id=completed_step_id,
                    failure_label=outcome_label,
                    recovery_kind=recovery_class.kind,
                )
                await _enqueue_start_step(s, wfx, wf, recovery_step_id, attempt=0)
                await s.commit()
                return

        # Tier-2 retry on failure — skipped when `retryable=False` (e.g. a
        # schema validation failure from CodingAgentCommand.handle_response:
        # retrying would produce the same bad output).
        if outcome_label == "failure" and retryable:
            attempts = _get_attempt(wfx, completed_step_id)
            if attempts < step.retry_policy.max_attempts - 1:
                wfx.state = WorkflowState.RUNNING.value
                _publish_state_changed(s, wfx)
                await _enqueue_start_step(s, wfx, wf, completed_step_id, attempt=attempts + 1)
                await s.commit()
                return

        # Evaluate the completing step's static transition.
        target: str | TerminalAction
        target = _resolve_transition(wf, step, outcome_label or "success")

        if target is TerminalAction.COMPLETE_WORKFLOW:
            if wfx.finalizer_fired and wf.finalizer is not None and completed_step_id == wf.finalizer.step_id:
                # Post-finalizer discrimination: cancel_requested is the sole
                # discriminator between CANCELLED and FAILED.
                if wfx.cancel_requested:
                    await _enter_cancelled_state(s, wfx, completed_step_id)
                    log.info(
                        "workflow.route_workflow.cancelled_post_finalizer",
                        workflow_execution_id=workflow_execution_id,
                        finalizer_step_id=completed_step_id,
                    )
                    await s.commit()
                    return
                target = TerminalAction.FAIL_WORKFLOW
            else:
                await _enter_terminal_state(s, wfx, WorkflowState.DONE)
                await s.commit()
                return

        if target is TerminalAction.FAIL_WORKFLOW:
            if wf.finalizer is not None and not wfx.finalizer_fired:
                wfx.finalizer_fired = True
                failure_reason = _extract_failure_reason(outputs, outcome_label)
                _store_pending_failure(wfx, completed_step_id, failure_reason)
                wfx.state = WorkflowState.RUNNING.value
                _publish_state_changed(s, wfx)
                log.debug(
                    "workflow.route_workflow.finalizer_dispatched",
                    workflow_execution_id=workflow_execution_id,
                    finalizer_step_id=wf.finalizer.step_id,
                    failed_step_id=completed_step_id,
                )
                await _enqueue_start_step(s, wfx, wf, wf.finalizer.step_id, attempt=0)
                await s.commit()
                return

            # Post-finalizer discrimination when the finalizer's transition was
            # FAIL_WORKFLOW (rather than COMPLETE_WORKFLOW). cancel_requested is
            # the sole discriminator between CANCELLED and FAILED.
            if (
                wfx.cancel_requested
                and wfx.finalizer_fired
                and wf.finalizer is not None
                and completed_step_id == wf.finalizer.step_id
            ):
                await _enter_cancelled_state(s, wfx, completed_step_id)
                log.info(
                    "workflow.route_workflow.cancelled_post_finalizer",
                    workflow_execution_id=workflow_execution_id,
                    finalizer_step_id=completed_step_id,
                )
                await s.commit()
                return

            failure_reason = _pop_pending_failure_reason(wfx) or _extract_failure_reason(
                outputs, outcome_label
            )
            failed_step_id = _pop_pending_failure_step(wfx) or completed_step_id
            wfx.failure_reason = failure_reason
            await _enter_terminal_state(s, wfx, WorkflowState.FAILED)
            from app.core.audit_log import Actor, audit  # noqa: PLC0415

            await audit(
                "workflow_execution",
                wfx.id,
                "workflow.failed",
                WorkflowFailedPayload(
                    workflow_execution_id=str(wfx.id),
                    ticket_id=str(wfx.ticket_id),
                    failed_step_id=failed_step_id,
                    failure_reason=failure_reason,
                ),
                Actor.system(),
                org_id=_workflow_org_id(wfx),
                session=s,
            )
            log.info(
                "workflow.route_workflow.failed",
                workflow_execution_id=workflow_execution_id,
                failed_step_id=failed_step_id,
                failure_reason=failure_reason,
            )
            await s.commit()
            return

        # Otherwise it's a string step id.
        wfx.state = WorkflowState.RUNNING.value
        _publish_state_changed(s, wfx)
        await _enqueue_start_step(s, wfx, wf, target, attempt=0)
        await s.commit()


# Export the task refs.
START_STEP: TaskRef = start_step
HANDLE_AGENT_EVENT: TaskRef = handle_agent_event
ROUTE_WORKFLOW: TaskRef = route_workflow


# ── Admin APIs ──────────────────────────────────────────────────────────


async def request_cancel(workflow_execution_id: str, *, session: AsyncSession) -> bool:
    """Set `cancel_requested=TRUE` on the workflow execution and (for states
    where no natural task tick will arrive soon) proactively enqueue
    `route_workflow` so the cancel is processed promptly.

    Returns True if the cancel flag was newly set; False if the workflow is
    already in a terminal state or not found.

    State branching:
    - PENDING / RUNNING / AWAITING_HUMAN → set flag + enqueue route_workflow
    - AWAITING_AGENT → set flag only; the agent's natural terminal event triggers
      the next route_workflow tick
    - terminal (DONE / FAILED / CANCELLED) → no-op, returns False
    """
    wfx = await _load_execution(session, workflow_execution_id)
    if wfx is None:
        return False
    current_state = WorkflowState(wfx.state)
    if current_state in TERMINAL_STATES:
        return False
    wfx.cancel_requested = True
    if current_state in _PROACTIVE_CANCEL_STATES:
        await enqueue(
            ROUTE_WORKFLOW,
            args={
                "workflow_execution_id": workflow_execution_id,
                "completed_step_id": None,
                "outcome_label": None,
                "outputs": {},
                "traceparent": wfx.otel_trace_context,
            },
            session=session,
        )
    return True


async def resume_hitl(
    workflow_execution_id: str,
    *,
    response: dict[str, Any],
    session: AsyncSession,
) -> bool:
    wfx = await _load_execution(session, workflow_execution_id)
    if wfx is None:
        raise WorkflowExecutionNotFoundError(workflow_execution_id)
    if wfx.state != WorkflowState.AWAITING_HUMAN.value:
        return False
    pending = (
        (
            await session.execute(
                select(PendingHumanDecisionRow).where(
                    PendingHumanDecisionRow.workflow_execution_id == wfx.id,
                    PendingHumanDecisionRow.resolved_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if pending is None:
        return False

    from datetime import UTC, datetime  # noqa: PLC0415

    pending.resolution_payload = response
    pending.resolved_at = datetime.now(UTC)
    wfx.state = WorkflowState.RUNNING.value
    _publish_state_changed(session, wfx)

    await enqueue(
        ROUTE_WORKFLOW,
        args={
            "workflow_execution_id": workflow_execution_id,
            "completed_step_id": wfx.current_step_id,
            "outcome_label": response.get("__label__", "success"),
            "outputs": response,
            "traceparent": wfx.otel_trace_context,
        },
        session=session,
    )
    return True


# ── Internal helpers ────────────────────────────────────────────────────


async def _load_execution(session: AsyncSession, workflow_execution_id: str) -> WorkflowExecutionRow | None:
    try:
        wid = UUID(workflow_execution_id)
    except TypeError, ValueError:
        return None
    return await session.get(WorkflowExecutionRow, wid)


def _resolve_step(wfx: WorkflowExecutionRow, wf: Workflow, step_id: str) -> StepRef | None:
    """Look up a step by id. Synthetic recovery steps (id prefix `_recover_`)
    are reconstructed from the recovered_steps column; all other step ids are
    resolved from the static workflow definition."""
    if step_id.startswith("_recover_"):
        # Recovery steps carry a deterministic id `_recover_{original_step_id}`.
        original_step_id = step_id[len("_recover_") :]
        failure_label = (wfx.recovered_steps or {}).get(original_step_id)
        if failure_label is None:
            return None
        # Look up the per-workflow recovery command class from the engine.
        recovery_class = get_engine()._get_recovery_class(wf.name, wf.version, failure_label)
        if recovery_class is None:
            return None
        return StepRef(
            command_class=recovery_class,
            step_id=step_id,
            inputs_factory=None,
            retry_policy=RetryPolicy(),
        )
    return wf.step_by_step_id(step_id)


def _resolve_transition(wf: Workflow, step: StepRef, outcome_label: str) -> str | TerminalAction:
    """Resolve the next target from the workflow's transitions map.

    Defaults: success → next listed step (or `complete_workflow` if step is last);
    failure → `fail_workflow`.

    Synthetic recovery steps (`_recover_{original_step_id}`) receive special
    routing: success returns control to the original step; any failure immediately
    fails the workflow."""
    if step.step_id.startswith("_recover_"):
        if outcome_label == "success":
            return step.step_id[len("_recover_") :]
        return TerminalAction.FAIL_WORKFLOW

    transitions: dict = wf.transitions or {}
    step_transitions: dict = transitions.get(step, {})
    explicit = step_transitions.get(outcome_label)
    if explicit is not None:
        # Explicit can be a StepRef or TerminalAction.
        if isinstance(explicit, StepRef):
            return explicit.step_id
        return explicit

    if outcome_label == "success":
        step_ids = [s.step_id for s in wf.steps]
        if step.step_id in step_ids:
            idx = step_ids.index(step.step_id)
            if idx + 1 < len(step_ids):
                return wf.steps[idx + 1].step_id
        return TerminalAction.COMPLETE_WORKFLOW
    return TerminalAction.FAIL_WORKFLOW


def _build_outputs_map(wfx: WorkflowExecutionRow, wf: Workflow) -> dict[str, BaseModel]:
    """Build the ContextVar map from step_state + workflow_input.

    For each workflow step that has an entry in step_state["step_id"]["outputs"],
    validate via `step.command_class.Outputs.model_validate(...)`.
    For the workflow_input, validate via `wf.workflow_input.snapshot_type.model_validate(...)`.
    """
    outputs_map: dict[str, BaseModel] = {}
    for s in wf.steps:
        entry = wfx.step_state.get(s.step_id)
        if isinstance(entry, dict) and "outputs" in entry:
            raw_outputs = entry["outputs"]
            if isinstance(raw_outputs, dict):
                try:
                    outputs_cls = s.command_class.Outputs  # type: ignore[attr-defined]
                    typed_out = outputs_cls.model_validate(raw_outputs)
                    outputs_map[s.step_id] = typed_out
                except Exception:
                    # Non-fatal: lambda may handle None gracefully via outputs_or_none.
                    pass
    if wf.workflow_input is not None and isinstance(wfx.workflow_input, dict):
        try:
            typed_wi = wf.workflow_input.snapshot_type.model_validate(wfx.workflow_input)
            outputs_map["__workflow_input__"] = typed_wi
        except Exception:
            pass
    return outputs_map


async def _enqueue_start_step(
    session: AsyncSession,
    wfx: WorkflowExecutionRow,
    wf: Workflow,
    step_id: str,
    *,
    attempt: int,
) -> None:
    """Evaluate the step's `inputs_factory` lambda (if any) against prior step
    outputs populated into `_step_outputs_var`, then enqueue `workflow.start_step`.
    """
    step = _resolve_step(wfx, wf, step_id)
    resolved_inputs: dict[str, Any] = {}

    if step is not None and step.inputs_factory is not None:
        outputs_map = _build_outputs_map(wfx, wf)
        token = _step_outputs_var.set(outputs_map)
        try:
            typed_inputs = step.inputs_factory()
            resolved_inputs = typed_inputs.model_dump(mode="json")
        except Exception as exc:
            log.warning(
                "workflow.enqueue_start_step.inputs_factory_failed",
                workflow_execution_id=str(wfx.id),
                step_id=step_id,
                error=str(exc),
            )
            # Proceed with empty inputs; the command's Inputs.model_validate({})
            # will fail at dispatch time if required fields are missing.
        finally:
            _step_outputs_var.reset(token)

    wfx.current_step_id = step_id

    await enqueue(
        START_STEP,
        args={
            "workflow_execution_id": str(wfx.id),
            "step_id": step_id,
            "attempt": attempt,
            "inputs": resolved_inputs,
            "traceparent": wfx.otel_trace_context,
        },
        session=session,
    )


def _has_recovered(wfx: WorkflowExecutionRow, step_id: str) -> bool:
    return step_id in (wfx.recovered_steps or {})


def _mark_recovered(wfx: WorkflowExecutionRow, step_id: str, failure_label: str) -> None:
    wfx.recovered_steps = {**wfx.recovered_steps, step_id: failure_label}


def _get_attempt(wfx: WorkflowExecutionRow, step_id: str) -> int:
    return int((wfx.step_attempts or {}).get(step_id, 0))


def _persist_outputs(
    wfx: WorkflowExecutionRow,
    step_id: str,
    outcome_label: str | None,
    outputs: dict[str, Any],
) -> None:
    bucket = dict(wfx.step_state)
    prior = bucket.get(step_id) if isinstance(bucket.get(step_id), dict) else None
    started_at = prior.get("started_at") if isinstance(prior, dict) else None
    step_entry: dict[str, Any] = {
        "outcome_label": outcome_label,
        "outputs": {k: v for k, v in outputs.items() if not k.startswith("__")},
        "completed_at": datetime.now(_UTC).isoformat(),
    }
    if started_at is not None:
        step_entry["started_at"] = started_at
    bucket[step_id] = step_entry
    wfx.step_state = bucket


def _stamp_step_started(wfx: WorkflowExecutionRow, step_id: str) -> None:
    bucket = dict(wfx.step_state)
    existing = bucket.get(step_id) if isinstance(bucket.get(step_id), dict) else None
    if isinstance(existing, dict) and "started_at" in existing:
        return
    entry: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    entry["started_at"] = datetime.now(_UTC).isoformat()
    bucket[step_id] = entry
    wfx.step_state = bucket


def _publish_state_changed(session: AsyncSession, wfx: WorkflowExecutionRow) -> None:
    org_id = _workflow_org_id(wfx)
    publish_general_after_commit(
        session,
        org_id=org_id,
        kind=GeneralEventKind.WORKFLOW_STATE_CHANGED,
        payload={
            "ticket_id": str(wfx.ticket_id),
            "workflow_execution_id": str(wfx.id),
            "state": wfx.state,
        },
    )


async def _enter_terminal_state(
    session: AsyncSession,
    wfx: WorkflowExecutionRow,
    new_state: WorkflowState,
) -> None:
    wfx.state = new_state.value
    _publish_state_changed(session, wfx)
    org_id = _workflow_org_id(wfx)
    try:
        wf = get_engine().get_workflow(wfx.workflow_name, version=wfx.workflow_version)
    except WorkflowNotFoundError:
        wf = None
    if wf is not None and wf.on_terminal is not None:
        with with_remote_parent_span(
            _tracer, "workflow.callback.on_terminal", wfx.otel_trace_context
        ) as cb_span:
            try:
                await wf.on_terminal(
                    workflow_execution_id=wfx.id,
                    workflow_name=wfx.workflow_name,
                    ticket_id=wfx.ticket_id,
                    org_id=org_id,
                    terminal_state=new_state,
                    failure_reason=wfx.failure_reason,
                    session=session,
                )
            except Exception as exc:
                cb_span.record_exception(exc)
                cb_span.set_status(StatusCode.ERROR, str(exc))
                raise


async def _enter_cancelled_state(
    session: AsyncSession,
    wfx: WorkflowExecutionRow,
    cancelled_step_id: str | None,
) -> None:
    """Enter CANCELLED terminal state and write the `workflow.cancelled` audit row.

    Always call `_enter_terminal_state` first (sets state + fires on_terminal callback
    + publishes SSE), then write the audit row inside the same transaction. The caller
    is responsible for committing.
    """
    await _enter_terminal_state(session, wfx, WorkflowState.CANCELLED)
    from app.core.audit_log import Actor, audit  # noqa: PLC0415

    await audit(
        "workflow_execution",
        wfx.id,
        "workflow.cancelled",
        WorkflowCancelledPayload(
            workflow_execution_id=str(wfx.id),
            ticket_id=str(wfx.ticket_id),
            cancelled_step_id=cancelled_step_id,
        ),
        Actor.system(),
        org_id=_workflow_org_id(wfx),
        session=session,
    )
    log.info(
        "workflow.cancelled",
        workflow_execution_id=str(wfx.id),
        cancelled_step_id=cancelled_step_id,
    )


def _outcome_payload(outcome: Outcome) -> dict[str, Any]:
    """Pack an Outcome's outputs into the routing task's `outputs` argument."""
    payload: dict[str, Any] = outcome.outputs.model_dump(mode="json")
    if outcome.failure_reason is not None:
        payload["__failure_reason__"] = outcome.failure_reason
    return payload


# ── Finalizer helpers ────────────────────────────────────────────────────


def _store_pending_failure(
    wfx: WorkflowExecutionRow,
    failed_step_id: str | None,
    failure_reason: str | None,
) -> None:
    if failed_step_id is not None:
        wfx.pending_failure_step_id = failed_step_id
    if failure_reason is not None:
        wfx.pending_failure_reason = failure_reason


def _pop_pending_failure_step(wfx: WorkflowExecutionRow) -> str | None:
    value = wfx.pending_failure_step_id
    wfx.pending_failure_step_id = None
    return value


def _pop_pending_failure_reason(wfx: WorkflowExecutionRow) -> str | None:
    value = wfx.pending_failure_reason
    wfx.pending_failure_reason = None
    return value


def _extract_failure_reason(outputs: dict[str, Any], outcome_label: str | None) -> str | None:
    reason = outputs.get("__failure_reason__")
    if reason:
        return str(reason)
    if outcome_label and outcome_label != "success":
        return outcome_label
    return None


def _workflow_org_id(wfx: WorkflowExecutionRow) -> UUID:
    from app.core.auth import current_org_id  # noqa: PLC0415

    org_id = current_org_id()
    if org_id is not None:
        return org_id
    payload = wfx.workflow_input
    if isinstance(payload, dict):
        raw = payload.get("org_id")
        if raw is not None:
            return UUID(str(raw))
    log.warning(
        "workflow.route_workflow.no_org_id_for_audit",
        workflow_execution_id=str(wfx.id),
    )
    return UUID(int=0)


async def _safe_execute(
    command: WorkflowCommand,
    inputs: BaseModel,
    ctx: CommandContext,
    traceparent: str | None = None,
    category: str = "local",
    session: object = None,
) -> Outcome:
    """Call command.execute(inputs, ctx) inside a `workflow.command.{kind}` child span.

    `category` is the span attribute value (``"local"`` or ``"hitl"``).
    `session` is passed as ``session=session`` kwarg when not None — LocalCommand
    Protocol declares it as a keyword-only argument; HITLCommand (ABC) does not take it.
    Typed `object` (not `AsyncSession | None`) so the semgrep session-discipline rule
    does not flag this private engine helper — callers are the sole source of truth on
    what gets passed.

    When `session` is not None (Local command): exceptions are re-raised after recording
    span error so the caller's SAVEPOINT handler can roll back the transaction.
    When `session` is None (HITL): exceptions are caught and converted to Outcome.failure.

    Runtime guard: asserts `session.in_transaction()` before and after execute() when
    session is not None — catches a LocalCommand that commits the outer transaction.
    Guard is best-effort: committing a nested SAVEPOINT is not detected (in_transaction
    remains True while the outer transaction lives).
    """
    outer_span = trace.get_current_span()
    kind = command.kind  # type: ignore[attr-defined]
    with with_remote_parent_span(_tracer, f"workflow.command.{kind}", traceparent) as child_span:
        child_span.set_attribute("command.kind", kind)
        child_span.set_attribute("command.category", category)
        child_span.set_attribute("workflow.step_id", ctx.step_id)
        child_span.set_attribute("workflow.attempt", ctx.attempt)
        try:
            if session is not None:
                # LocalCommand: verify the session is in a transaction before and after.
                if not session.in_transaction():  # type: ignore[attr-defined]
                    raise RuntimeError(f"LocalCommand '{kind}' invoked outside a transaction — engine bug")
                outcome = await command.execute(inputs, ctx, session=session)  # type: ignore[arg-type,call-arg]
                if not session.in_transaction():  # type: ignore[attr-defined]
                    raise RuntimeError(
                        f"LocalCommand '{kind}' committed or closed the session — must never commit"
                    )
            else:
                outcome = await command.execute(inputs, ctx)  # type: ignore[arg-type]
        except Exception as exc:
            child_span.record_exception(exc)
            child_span.set_status(StatusCode.ERROR, str(exc))
            outer_span.set_status(StatusCode.ERROR, str(exc))
            log.exception(
                "workflow.command.raised",
                workflow_execution_id=ctx.workflow_execution_id,
                step_id=ctx.step_id,
            )
            if session is not None:
                # LocalCommand raised: re-raise so the caller's savepoint rolls back.
                raise
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        if outcome.kind is OutcomeKind.FAILURE:
            reason = outcome.failure_reason or "failure"
            child_span.set_status(StatusCode.ERROR, reason)
            outer_span.set_status(StatusCode.ERROR, reason)

    return outcome


# ── Engine ──────────────────────────────────────────────────────────────


class WorkflowEngine:
    """Workflow + WorkflowCommand registry. Process-singleton via `get_engine()`.

    `register_workflow(wf)` validates the workflow definition, auto-discovers
    all command classes from the steps tuple (instantiating each to build the
    registry entry), and runs lambda validation for steps that have
    `inputs_factory`.

    `start(workflow_name, ticket_id, *, workflow_input, session)` opens a
    workflow execution and enqueues the initial routing task.
    """

    def __init__(self) -> None:
        self._workflows: dict[tuple[str, int], Workflow] = {}
        self._commands: dict[str, WorkflowCommand] = {}
        # Per-workflow recovery map: (name, version) → {failure_label → command_class}.
        # Built at register_workflow time from wf.recovery_commands.
        self._recovery_maps: dict[tuple[str, int], dict[str, type]] = {}

    def register_workflow(self, wf: Workflow) -> None:
        key = (wf.name, wf.version)
        if key in self._workflows:
            raise WorkflowError(f"workflow '{wf.name}' v{wf.version} already registered")

        # Validate entry step exists in steps tuple.
        if wf.step_by_step_id(wf.entry.step_id) is None:
            raise WorkflowError(f"workflow '{wf.name}' entry step '{wf.entry.step_id}' not in steps")

        # Validate explicit transition targets.
        transitions: dict = wf.transitions or {}
        for _src_step, label_map in transitions.items():
            for label, target in label_map.items():
                if isinstance(target, TerminalAction):
                    continue
                if isinstance(target, StepRef):
                    if wf.step_by_step_id(target.step_id) is None:
                        raise WorkflowError(
                            f"workflow '{wf.name}' transition target '{target.step_id}' "
                            f"for label '{label}' not in steps"
                        )
                elif isinstance(target, str):
                    if wf.step_by_step_id(target) is None:
                        raise WorkflowError(
                            f"workflow '{wf.name}' transition target '{target}' "
                            f"for label '{label}' not in steps"
                        )

        # Auto-discover command classes from steps and register instances.
        for s in wf.steps:
            cmd_class = s.command_class
            if not hasattr(cmd_class, "kind"):
                raise WorkflowError(
                    f"workflow '{wf.name}' step '{s.step_id}': command class "
                    f"'{cmd_class.__name__}' is missing a `kind` class attribute"
                )
            kind = cast(_CommandClassProto, cmd_class).kind
            if kind not in self._commands:
                try:
                    instance = cmd_class()
                except Exception as exc:
                    raise WorkflowError(
                        f"workflow '{wf.name}' step '{s.step_id}': could not instantiate "
                        f"command class '{cmd_class.__name__}': {exc}"
                    ) from exc
                self._commands[kind] = instance

        # Validate inputs_factory lambdas catch field-name typos.
        #
        # `model_construct()` without kwargs leaves required fields unset —
        # accessing them raises AttributeError, the same signal we use to
        # detect "typo'd field name". We set each field to `None` so that
        # valid single-level access (`outputs.field`) succeeds; only truly
        # absent attribute names raise AttributeError.
        #
        # For fields whose annotation is itself a BaseModel subclass we
        # recursively call `_mock` so that nested access
        # (`outputs.response.findings`) also succeeds — without this,
        # `response=None` would make `None.findings` raise AttributeError
        # and be mis-identified as a field-name typo.
        def _mock(cls: type, _depth: int = 0) -> BaseModel:
            if _depth > 5:  # guard against pathological recursion
                return Empty()
            try:
                fields: dict[str, object] = {}
                for name, finfo in cls.model_fields.items():  # type: ignore[attr-defined]
                    ann = finfo.annotation
                    if isinstance(ann, type) and issubclass(ann, BaseModel):
                        fields[name] = _mock(ann, _depth + 1)
                    else:
                        fields[name] = None
                return cls.model_construct(**fields)  # type: ignore[attr-defined]
            except Exception:
                return Empty()

        mock_outputs: dict[str, BaseModel] = {}
        for s in wf.steps:
            try:
                outputs_cls = s.command_class.Outputs  # type: ignore[attr-defined]
                mock_outputs[s.step_id] = _mock(outputs_cls)
            except Exception:
                mock_outputs[s.step_id] = Empty()
        if wf.workflow_input is not None:
            try:
                mock_outputs["__workflow_input__"] = _mock(wf.workflow_input.snapshot_type)
            except Exception:
                mock_outputs["__workflow_input__"] = Empty()

        for s in wf.steps:
            if s.inputs_factory is None:
                continue
            token = _step_outputs_var.set(mock_outputs)
            try:
                s.inputs_factory()
            except AttributeError as exc:
                raise WorkflowValidationError(
                    f"workflow '{wf.name}' step '{s.step_id}' inputs lambda references unknown field: {exc}"
                ) from exc
            except Exception:
                # KeyError (step not in map yet), ValidationError, etc. are runtime
                # concerns — not lambda typos. Let them pass registration.
                pass
            finally:
                _step_outputs_var.reset(token)

        # Walk recovery_commands: build the per-workflow failure-label → class map
        # and auto-register each recovery command instance.
        recovery_map: dict[str, type] = {}
        for rc_class in wf.recovery_commands:
            if not hasattr(rc_class, "recovers_failure_label"):
                raise WorkflowError(
                    f"workflow '{wf.name}' recovery command '{rc_class.__name__}' "
                    f"is missing a `recovers_failure_label` class attribute"
                )
            label = cast(_RecoveryCommandClassProto, rc_class).recovers_failure_label
            if label in recovery_map:
                raise WorkflowError(
                    f"workflow '{wf.name}' has duplicate recovery label '{label}' "
                    f"(from '{rc_class.__name__}' and '{recovery_map[label].__name__}')"
                )
            recovery_map[label] = rc_class
            # Auto-register the command instance so the engine can dispatch it.
            if not hasattr(rc_class, "kind"):
                raise WorkflowError(
                    f"workflow '{wf.name}' recovery command '{rc_class.__name__}' "
                    f"is missing a `kind` class attribute"
                )
            rc_kind = cast(_CommandClassProto, rc_class).kind
            if rc_kind not in self._commands:
                try:
                    instance = rc_class()
                except Exception as exc:
                    raise WorkflowError(
                        f"workflow '{wf.name}' recovery command '{rc_class.__name__}': "
                        f"could not instantiate: {exc}"
                    ) from exc
                self._commands[rc_kind] = instance  # type: ignore[assignment]

        self._recovery_maps[key] = recovery_map
        self._workflows[key] = wf

    def _get_recovery_class(self, name: str, version: int, failure_label: str) -> type | None:
        """Return the recovery command class for `failure_label` in the given workflow,
        or None when no recovery policy is declared."""
        return self._recovery_maps.get((name, version), {}).get(failure_label)

    def get_workflow(self, name: str, version: int | None = None) -> Workflow:
        if version is not None:
            wf = self._workflows.get((name, version))
            if wf is None:
                raise WorkflowNotFoundError(f"workflow '{name}' v{version} not registered")
            return wf
        matches = [w for (n, _), w in self._workflows.items() if n == name]
        if not matches:
            raise WorkflowNotFoundError(f"workflow '{name}' not registered")
        return max(matches, key=lambda w: w.version)

    def get_command(self, kind: str) -> WorkflowCommand:
        cmd = self._commands.get(kind)
        if cmd is None:
            raise CommandNotRegisteredError(f"WorkflowCommand kind '{kind}' not registered")
        return cmd

    def registered_workflow_names(self) -> list[str]:
        return sorted({name for (name, _) in self._workflows})

    def registered_command_kinds(self) -> list[str]:
        return sorted(self._commands.keys())

    async def start(
        self,
        *,
        workflow_name: str,
        ticket_id: str,
        version: int | None = None,
        traceparent: str | None = None,
        workflow_input: BaseModel | None = None,
        session: AsyncSession,
    ) -> str:
        """Create a `workflow_executions` row in `pending` state, enqueue the
        initial `route_workflow` task, and return the new execution id.

        `workflow_input`: typed BaseModel snapshot for the workflow. If the
        registered workflow declares `workflow_input: WorkflowInputRef`, the
        snapshot is serialised via `model_dump(mode='json')` and stored on the row;
        the engine validates that the supplied type matches the declared type.

        Raises `WorkflowError` when `workflow_input` type doesn't match the
        workflow's declared `workflow_input.snapshot_type`.
        """
        wf = self.get_workflow(workflow_name, version=version)

        # Validate workflow_input type if the workflow declares one.
        if wf.workflow_input is not None and workflow_input is not None:
            if not isinstance(workflow_input, wf.workflow_input.snapshot_type):
                raise WorkflowError(
                    f"workflow '{workflow_name}' expects workflow_input of type "
                    f"'{wf.workflow_input.snapshot_type.__name__}', "
                    f"got '{type(workflow_input).__name__}'"
                )

        with with_remote_parent_span(_tracer, f"workflow.run.{wf.name}", traceparent) as run_span:
            run_traceparent = current_traceparent()

            wi_dict = workflow_input.model_dump(mode="json") if workflow_input is not None else None

            row = WorkflowExecutionRow(
                ticket_id=ticket_id,
                workflow_name=wf.name,
                workflow_version=wf.version,
                state=WorkflowState.PENDING.value,
                current_step_id=None,
                pending_agent_command_id=None,
                step_state={},
                cancel_requested=False,
                otel_trace_context=run_traceparent,
                workflow_input=wi_dict,
            )
            session.add(row)
            await session.flush()

            run_span.set_attribute("workflow.name", wf.name)
            run_span.set_attribute("workflow.version", wf.version)
            run_span.set_attribute("workflow.execution_id", str(row.id))

            await enqueue(
                ROUTE_WORKFLOW,
                args={
                    "workflow_execution_id": str(row.id),
                    "completed_step_id": None,
                    "outcome_label": None,
                    "outputs": {},
                    "traceparent": run_traceparent,
                },
                session=session,
            )
            log.info(
                "workflow.started",
                workflow_execution_id=str(row.id),
                workflow_name=wf.name,
                workflow_version=wf.version,
                ticket_id=ticket_id,
            )
            return str(row.id)


_engine: WorkflowEngine | None = None


def get_engine() -> WorkflowEngine:
    """Process-singleton engine."""
    global _engine
    if _engine is None:
        _engine = WorkflowEngine()
    return _engine


def register_workflow(wf: Workflow) -> None:
    """Register a workflow spec on the process-singleton engine."""
    get_engine().register_workflow(wf)


def bind_engine(instance: WorkflowEngine | None) -> WorkflowEngine | None:
    """Swap the process-singleton engine. Returns the prior engine (or None if
    no engine was bound). Test-only public seam — production never calls this.

    Used by `app/testing/workflow_harness.scoped_engine` to install a fresh
    engine for a single test scope.
    """
    global _engine
    prior = _engine
    _engine = instance
    return prior


def unregister_workflow(workflow_name: str, version: int) -> None:
    """Remove a workflow from the process-singleton engine by name + version.
    Test-only public seam — production never calls this.

    Used by `app/testing/workflow_harness.scoped_workflow` to clean up a
    workflow registration on scope exit.
    """
    key = (workflow_name, version)
    engine = get_engine()
    engine._workflows.pop(key, None)
    engine._recovery_maps.pop(key, None)
