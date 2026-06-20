"""`WorkflowEngine` + the three core/tasks task bodies.

Provides:
- `start_step` body — branches on command category. Local + HITL execute
  inline; Workspace calls `command.dispatch(inputs, ctx, session=s)` to enqueue
  an AgentCommand row, parks the workflow in `awaiting_agent`, and stores
  the returned `command_id` as `pending_agent_command_id`.
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

from dataclasses import dataclass
from datetime import UTC as _UTC
from datetime import datetime
from typing import Any
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
from app.core.workflow.recovery import get_recovery_policy
from app.core.workflow.start_hooks import get_start_hooks
from app.core.workflow.terminal_hooks import get_terminal_hooks
from app.core.workflow.types import (
    TERMINAL_STATES,
    CommandCategory,
    CommandContext,
    CommandNotRegisteredError,
    Empty,
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
    WorkspaceWorkflowCommand,
    _step_outputs_var,
)

log = structlog.get_logger("core.workflow")
_tracer = trace.get_tracer("core.workflow")


class WorkflowFailedPayload(BaseModel):
    """Audit payload for `workflow.failed` rows written by the engine on
    terminal-fail. Generic — owned by `core/workflow`, not domain-specific."""

    workflow_execution_id: str
    ticket_id: str
    failed_step_id: str | None
    failure_reason: str | None


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

    - **Workspace** (`isinstance(cmd, WorkspaceWorkflowCommand)`) — calls
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

        # Cancellation check — set the row terminal and exit before dispatch.
        if wfx.cancel_requested:
            await _enter_terminal_state(s, wfx, WorkflowState.CANCELLED)
            log.debug(
                "workflow.start_step.cancelled_pre_dispatch", workflow_execution_id=workflow_execution_id
            )
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

        wfx.current_step_id = step_id
        _stamp_step_started(wfx, step_id)

        if isinstance(command, WorkspaceWorkflowCommand):
            # Workspace commands enqueue an AgentCommand durably inside this
            # transaction via the command's own `dispatch` method. The engine
            # parks the execution in `awaiting_agent` and stores the returned
            # `command_id` as `pending_agent_command_id`. The terminal
            # AgentEvent arrives via `handle_agent_event` to resume routing.
            outer_span = trace.get_current_span()
            kind = command_kind
            with with_remote_parent_span(
                _tracer, f"workflow.command.{kind}", wfx.otel_trace_context
            ) as cmd_span:
                cmd_span.set_attribute("command.kind", kind)
                cmd_span.set_attribute("command.category", "workspace")
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
                try:
                    command_id = await command.dispatch(typed_inputs, ws_cmd_ctx, session=s)
                except Exception as exc:
                    cmd_span.record_exception(exc)
                    cmd_span.set_status(StatusCode.ERROR, str(exc))
                    outer_span.set_status(StatusCode.ERROR, str(exc))
                    log.exception(
                        "workflow.command.workspace_dispatch_raised",
                        workflow_execution_id=workflow_execution_id,
                        step_id=step_id,
                    )
                    await _enter_terminal_state(s, wfx, WorkflowState.FAILED)
                    await s.commit()
                    return
            wfx.pending_agent_command_id = command_id
            wfx.state = WorkflowState.AWAITING_AGENT.value
            _publish_state_changed(s, wfx)
            log.debug(
                "workflow.start_step.workspace_dispatched",
                workflow_execution_id=workflow_execution_id,
                command_kind=command_kind,
                command_id=str(command_id),
            )
            await s.commit()
            return

        # Local + HITL: run execute() inline.
        outcome = await _safe_execute(command, typed_inputs, cmd_ctx, traceparent=wfx.otel_trace_context)

        if getattr(command, "category", CommandCategory.LOCAL) == CommandCategory.HITL:
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

        # Local command — persist outcome + enqueue route_workflow.
        wfx.step_attempts = {**wfx.step_attempts, step_id: attempt}
        await enqueue(
            ROUTE_WORKFLOW,
            args={
                "workflow_execution_id": workflow_execution_id,
                "completed_step_id": step_id,
                "outcome_label": outcome.label,
                "outputs": _outcome_payload(outcome),
                "traceparent": wfx.otel_trace_context,
            },
            session=s,
        )
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

        await enqueue(
            ROUTE_WORKFLOW,
            args={
                "workflow_execution_id": workflow_execution_id,
                "completed_step_id": completed_step_id,
                "outcome_label": outcome_label,
                "outputs": outputs,
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

        if wfx.cancel_requested:
            await _enter_terminal_state(s, wfx, WorkflowState.CANCELLED)
            log.debug(
                "workflow.route_workflow.cancelled_at_route",
                workflow_execution_id=workflow_execution_id,
            )
            await s.commit()
            return

        engine = get_engine()
        wf = engine.get_workflow(wfx.workflow_name, version=wfx.workflow_version)

        # Initial call from start(): no completed step. Bootstrap by
        # enqueueing the entry step.
        if completed_step_id is None:
            wfx.state = WorkflowState.RUNNING.value
            _publish_state_changed(s, wfx)
            org_id = _workflow_org_id(wfx)
            for hook in get_start_hooks():
                await hook(
                    workflow_execution_id=wfx.id,
                    workflow_name=wfx.workflow_name,
                    ticket_id=wfx.ticket_id,
                    org_id=org_id,
                    session=s,
                )
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

        # Tier-1 recovery: if the failure label has a registered recovery
        # policy (e.g. `auth_expired → RefreshWorkspaceAuth`), insert the
        # recovery command as a synthetic step that runs BEFORE the original
        # step retries. Recovery fires at most once per step instance.
        if outcome_label and outcome_label != "success":
            recovery_kind = get_recovery_policy(outcome_label)
            if recovery_kind is not None and not _has_recovered(wfx, completed_step_id):
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
                    recovery_kind=recovery_kind,
                )
                await _enqueue_start_step(s, wfx, wf, recovery_step_id, attempt=0)
                await s.commit()
                return

        # Tier-2 retry on failure.
        if outcome_label == "failure":
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
    wfx = await _load_execution(session, workflow_execution_id)
    if wfx is None:
        return False
    if WorkflowState(wfx.state) in TERMINAL_STATES:
        return False
    wfx.cancel_requested = True
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


# ── Read projections ────────────────────────────────────────────────────


@dataclass(frozen=True)
class WorkflowExecutionSummary:
    id: UUID
    ticket_id: UUID
    workflow_name: str
    state: str
    current_step_id: str | None
    created_at: datetime
    updated_at: datetime
    pending_agent_command_id: UUID | None = None
    cancel_requested: bool = False
    otel_trace_context: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class HitlHistoryEntry:
    id: UUID
    workflow_execution_id: UUID
    question_payload: dict[str, Any]
    resolution_payload: dict[str, Any] | None
    resolved_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class WorkflowStepSummary:
    step_id: str
    command_kind: str
    state: str
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class WorkflowRunView:
    id: UUID
    workflow_name: str
    workflow_version: int
    state: str
    current_step_id: str | None
    created_at: datetime
    updated_at: datetime
    steps: tuple[WorkflowStepSummary, ...]
    failure_reason: str | None = None


def _project_execution(row: WorkflowExecutionRow) -> WorkflowExecutionSummary:
    return WorkflowExecutionSummary(
        id=row.id,
        ticket_id=row.ticket_id,
        workflow_name=row.workflow_name,
        state=row.state,
        current_step_id=row.current_step_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        pending_agent_command_id=row.pending_agent_command_id,
        cancel_requested=row.cancel_requested,
        otel_trace_context=row.otel_trace_context,
        failure_reason=row.failure_reason,
    )


async def list_executions_for_ticket(
    ticket_id: UUID, *, session: AsyncSession
) -> list[WorkflowExecutionSummary]:
    from sqlalchemy import desc  # noqa: PLC0415

    rows = (
        (
            await session.execute(
                select(WorkflowExecutionRow)
                .where(WorkflowExecutionRow.ticket_id == ticket_id)
                .order_by(desc(WorkflowExecutionRow.created_at))
            )
        )
        .scalars()
        .all()
    )
    return [_project_execution(r) for r in rows]


async def get_execution_summary(
    execution_id: UUID, *, session: AsyncSession
) -> WorkflowExecutionSummary | None:
    row = await session.get(WorkflowExecutionRow, execution_id)
    if row is None:
        return None
    return _project_execution(row)


async def get_awaiting_human_execution(
    ticket_id: UUID, *, session: AsyncSession
) -> WorkflowExecutionSummary | None:
    from sqlalchemy import desc  # noqa: PLC0415

    row = (
        await session.execute(
            select(WorkflowExecutionRow)
            .where(
                WorkflowExecutionRow.ticket_id == ticket_id,
                WorkflowExecutionRow.state == WorkflowState.AWAITING_HUMAN.value,
            )
            .order_by(desc(WorkflowExecutionRow.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return _project_execution(row)


async def list_active_execution_ids(ticket_id: UUID, *, session: AsyncSession) -> list[UUID]:
    rows = (
        (
            await session.execute(
                select(WorkflowExecutionRow.id).where(
                    WorkflowExecutionRow.ticket_id == ticket_id,
                    WorkflowExecutionRow.state.notin_([st.value for st in TERMINAL_STATES]),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def list_hitl_history(ticket_id: UUID, *, session: AsyncSession) -> list[HitlHistoryEntry]:
    from sqlalchemy import desc  # noqa: PLC0415

    wfx_ids = (
        (
            await session.execute(
                select(WorkflowExecutionRow.id).where(WorkflowExecutionRow.ticket_id == ticket_id)
            )
        )
        .scalars()
        .all()
    )
    if not wfx_ids:
        return []
    rows = (
        (
            await session.execute(
                select(PendingHumanDecisionRow)
                .where(PendingHumanDecisionRow.workflow_execution_id.in_(wfx_ids))
                .order_by(desc(PendingHumanDecisionRow.created_at))
            )
        )
        .scalars()
        .all()
    )
    return [
        HitlHistoryEntry(
            id=r.id,
            workflow_execution_id=r.workflow_execution_id,
            question_payload=r.question_payload,
            resolution_payload=r.resolution_payload,
            resolved_at=r.resolved_at,
            created_at=r.created_at,
        )
        for r in rows
    ]


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _step_summary(
    step: StepRef,
    *,
    is_current: bool,
    entry: dict[str, Any] | None,
    execution_state: str,
) -> WorkflowStepSummary:
    started = _parse_iso(entry.get("started_at")) if isinstance(entry, dict) else None
    completed = _parse_iso(entry.get("completed_at")) if isinstance(entry, dict) else None
    outcome_label: str | None = entry.get("outcome_label") if isinstance(entry, dict) else None

    if outcome_label == "success":
        state = "done"
    elif outcome_label is not None:
        state = "skipped" if outcome_label == "_skipped" else "failed"
    elif is_current and execution_state in {
        WorkflowState.RUNNING.value,
        WorkflowState.AWAITING_AGENT.value,
        WorkflowState.AWAITING_HUMAN.value,
    }:
        state = "running"
    else:
        state = "pending"

    return WorkflowStepSummary(
        step_id=step.step_id,
        command_kind=step.command_class.kind,
        state=state,
        started_at=started,
        completed_at=completed,
    )


def _project_run_view(row: WorkflowExecutionRow) -> WorkflowRunView:
    engine = get_engine()
    try:
        wf = engine.get_workflow(row.workflow_name, version=row.workflow_version)
    except WorkflowNotFoundError:
        steps: tuple[WorkflowStepSummary, ...] = ()
    else:
        summaries: list[WorkflowStepSummary] = []
        for step in wf.steps:
            entry = row.step_state.get(step.step_id)
            if not isinstance(entry, dict):
                entry = None
            summaries.append(
                _step_summary(
                    step,
                    is_current=(row.current_step_id == step.step_id),
                    entry=entry,
                    execution_state=row.state,
                )
            )
        steps = tuple(summaries)
    return WorkflowRunView(
        id=row.id,
        workflow_name=row.workflow_name,
        workflow_version=row.workflow_version,
        state=row.state,
        current_step_id=row.current_step_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        steps=steps,
        failure_reason=row.failure_reason,
    )


async def list_run_views_for_ticket(ticket_id: UUID, *, session: AsyncSession) -> list[WorkflowRunView]:
    from sqlalchemy import desc  # noqa: PLC0415

    rows = (
        (
            await session.execute(
                select(WorkflowExecutionRow)
                .where(WorkflowExecutionRow.ticket_id == ticket_id)
                .order_by(desc(WorkflowExecutionRow.created_at))
            )
        )
        .scalars()
        .all()
    )
    return [_project_run_view(r) for r in rows]


async def list_all_execution_states(*, session: AsyncSession) -> list[str]:
    return list((await session.execute(select(WorkflowExecutionRow.state))).scalars().all())


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
        recovery_kind = get_recovery_policy(failure_label)
        if recovery_kind is None:
            return None
        # Look up the recovery command from the engine registry and create a synthetic StepRef.
        try:
            recovery_cmd = get_engine().get_command(recovery_kind)
        except CommandNotRegisteredError:
            return None
        return StepRef(
            command_class=type(recovery_cmd),
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
    for hook in get_terminal_hooks():
        await hook(
            workflow_execution_id=wfx.id,
            workflow_name=wfx.workflow_name,
            ticket_id=wfx.ticket_id,
            org_id=org_id,
            terminal_state=new_state,
            failure_reason=wfx.failure_reason,
            session=session,
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
) -> Outcome:
    """Call command.execute(inputs, ctx) inside a `workflow.command.{kind}` child span."""
    outer_span = trace.get_current_span()
    kind = command.kind  # type: ignore[attr-defined]
    cat = getattr(command, "category", CommandCategory.LOCAL)
    cat_value = cat.value if isinstance(cat, CommandCategory) else str(cat)
    with with_remote_parent_span(_tracer, f"workflow.command.{kind}", traceparent) as child_span:
        child_span.set_attribute("command.kind", kind)
        child_span.set_attribute("command.category", cat_value)
        child_span.set_attribute("workflow.step_id", ctx.step_id)
        child_span.set_attribute("workflow.attempt", ctx.attempt)
        try:
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

    `register_command(command)` is available for recovery commands that are
    not part of any workflow's step list (e.g. `RefreshWorkspaceAuth` which
    is only triggered by the recovery policy machinery).

    `start(workflow_name, ticket_id, *, workflow_input, session)` opens a
    workflow execution and enqueues the initial routing task.
    """

    def __init__(self) -> None:
        self._workflows: dict[tuple[str, int], Workflow] = {}
        self._commands: dict[str, WorkflowCommand] = {}

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
        # Read `kind` from the class attribute first so pre-registered commands
        # (e.g. test commands with constructor args registered via `register_command`)
        # are not re-instantiated — only register when the kind is absent.
        for s in wf.steps:
            cmd_class = s.command_class
            kind = getattr(cmd_class, "kind", None)  # type: ignore[attr-defined]
            if kind is None:
                raise WorkflowError(
                    f"workflow '{wf.name}' step '{s.step_id}': command class "
                    f"'{cmd_class.__name__}' is missing a `kind` class attribute"
                )
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
        # detect "typo'd field name". Passing `None` for every declared field
        # pre-sets them so that valid field access succeeds; only truly absent
        # attribute names then raise AttributeError, which is the right gate.
        def _mock(cls: type) -> BaseModel:
            try:
                fields = {name: None for name in cls.model_fields}  # type: ignore[attr-defined]
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

        self._workflows[key] = wf

    def register_command(self, command: WorkflowCommand) -> None:
        """Register a command that is not auto-discoverable from any workflow step.

        Used for recovery commands (e.g. `RefreshWorkspaceAuth`) that are only
        dispatched by the recovery policy machinery, not listed in any step.
        Raises `WorkflowError` if the kind is already registered with a different
        instance class.
        """
        kind = command.kind  # type: ignore[attr-defined]
        if kind in self._commands:
            existing = self._commands[kind]
            if type(existing) is not type(command):
                raise WorkflowError(
                    f"WorkflowCommand kind '{kind}' already registered with class {type(existing).__name__}"
                )
            return  # Same class — idempotent, allow.
        self._commands[kind] = command

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


def bind_engine(instance: WorkflowEngine | None) -> WorkflowEngine | None:
    global _engine
    prior = _engine
    _engine = instance
    return prior


def register_workflow(wf: Workflow) -> None:
    """Register a workflow spec on the process-singleton engine."""
    get_engine().register_workflow(wf)


def unregister_workflow(workflow_name: str, version: int) -> None:
    """Remove a workflow from the process-singleton engine by name + version."""
    key = (workflow_name, version)
    get_engine()._workflows.pop(key, None)
