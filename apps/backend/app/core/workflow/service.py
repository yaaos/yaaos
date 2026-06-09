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
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import session as db_session
from app.core.observability import with_remote_parent_span
from app.core.sse import GeneralEventKind, publish_general_after_commit
from app.core.tasks import TaskRef, enqueue, task
from app.core.workflow.models import PendingHumanDecisionRow, WorkflowExecutionRow
from app.core.workflow.recovery import get_recovery_policy
from app.core.workflow.terminal_hooks import get_terminal_hooks
from app.core.workflow.types import (
    TERMINAL_STATES,
    CommandCategory,
    CommandContext,
    CommandNotRegisteredError,
    Outcome,
    OutcomeKind,
    Step,
    TerminalAction,
    Workflow,
    WorkflowCommand,
    WorkflowError,
    WorkflowExecutionNotFoundError,
    WorkflowNotFoundError,
    WorkflowState,
    WorkspaceWorkflowCommand,
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


# Key prefix in WorkflowExecutionRow.step_state used to persist append_steps
# inserted dynamically by a Command's outcome. Anything not under this key is
# a step-id mapping (step_id → outcome+outputs).
_APPEND_QUEUE_KEY = "__append_queue__"
_APPENDED_POOL_KEY = "__appended_pool__"
_AFTER_APPEND_KEY = "__after_append__"
_ATTEMPTS_KEY = "__attempts__"
_RECOVERED_STEPS_KEY = "__recovered_steps__"
_TICKET_PAYLOAD_KEY = "__ticket_payload__"
# Marks that the workflow's declared finalizer has already been dispatched once,
# preventing double-fire on the failure path.
_FINALIZER_FIRED_KEY = "__finalizer_fired__"


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
    """Dispatch the step. Branches on the WorkflowCommand category:

    - **Workspace** — calls `command.dispatch(inputs, ctx, session=s)` to
      enqueue an AgentCommand row, parks the workflow in `awaiting_agent`,
      and sets `pending_agent_command_id` to the returned `command_id`.
    - **Local** — runs the command inline, persists its outcome, enqueues
      `route_workflow` via the outbox in the same transaction.
    - **HITL** — runs the command (which must return `Outcome.hitl_pending`),
      writes the `pending_human_decisions` row, sets `state = awaiting_human`.

    Span: emits a `workflow.start_step` span whose parent is the upstream
    span encoded in `traceparent`, so all task bodies in one workflow run
    nest under the same trace ID. The span sets the `workflow_execution_id`
    + `step_id` + `attempt` attributes for observability.
    """
    with with_remote_parent_span(_tracer, "workflow.start_step", traceparent) as span:
        span.set_attribute("workflow.execution_id", workflow_execution_id)
        span.set_attribute("workflow.step_id", step_id)
        span.set_attribute("workflow.attempt", attempt)
        await _start_step_impl(
            workflow_execution_id=workflow_execution_id,
            step_id=step_id,
            attempt=attempt,
            inputs=inputs,
            traceparent=traceparent,
        )


async def _start_step_impl(
    *,
    workflow_execution_id: str,
    step_id: str,
    attempt: int,
    inputs: dict[str, Any],
    traceparent: str | None,
) -> None:
    async with db_session() as s:
        wfx = await _load_execution(s, workflow_execution_id)
        if wfx is None:
            log.warning("workflow.start_step.unknown_execution", workflow_execution_id=workflow_execution_id)
            return

        # Cancellation check — set the row terminal and exit before dispatch.
        if wfx.cancel_requested:
            await _enter_terminal_state(s, wfx, WorkflowState.CANCELLED)
            log.info(
                "workflow.start_step.cancelled_pre_dispatch", workflow_execution_id=workflow_execution_id
            )
            await s.commit()
            return

        # State guard. start_step is only valid while running.
        if wfx.state != WorkflowState.RUNNING.value:
            log.info(
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

        try:
            command = engine.get_command(step.command_kind)
        except CommandNotRegisteredError:
            log.error(
                "workflow.start_step.command_not_registered",
                workflow_execution_id=workflow_execution_id,
                command_kind=step.command_kind,
            )
            await _enter_terminal_state(s, wfx, WorkflowState.FAILED)
            await s.commit()
            return

        cmd_ctx = CommandContext(
            workflow_execution_id=str(wfx.id),
            ticket_id=str(wfx.ticket_id),
            step_id=step_id,
            attempt=attempt,
            traceparent=traceparent,
        )

        wfx.current_step_id = step_id
        _stamp_step_started(wfx, step_id)

        if command.category == CommandCategory.WORKSPACE:
            # Workspace commands enqueue an AgentCommand durably inside this
            # transaction via the command's own `dispatch` method. The engine
            # parks the execution in `awaiting_agent` and stores the returned
            # `command_id` as `pending_agent_command_id`. The terminal
            # AgentEvent arrives via `handle_agent_event` to resume routing.
            # The engine remains ignorant of workspace internals — the command
            # owns the enqueue + repository wiring; the engine only handles
            # the state transition.
            if not isinstance(command, WorkspaceWorkflowCommand):
                raise WorkflowError(
                    f"WorkflowCommand kind '{step.command_kind}' has category=workspace "
                    f"but does not implement WorkspaceWorkflowCommand.dispatch"
                )
            command_id = await command.dispatch(inputs, cmd_ctx, session=s)
            wfx.pending_agent_command_id = command_id
            wfx.state = WorkflowState.AWAITING_AGENT.value
            _publish_state_changed(s, wfx)
            log.info(
                "workflow.start_step.workspace_dispatched",
                workflow_execution_id=workflow_execution_id,
                command_kind=step.command_kind,
                command_id=str(command_id),
            )
            await s.commit()
            return

        # Local + HITL: run execute() inline.
        outcome = await _safe_execute(command, inputs, cmd_ctx)

        if command.category == CommandCategory.HITL:
            if outcome.kind is not OutcomeKind.HITL_PENDING:
                # A HITL-category command must return hitl_pending. Treat
                # any other outcome as a programming bug and fail loud.
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
                    question_payload=dict(outcome.hitl_question or {}),
                )
            )
            wfx.state = WorkflowState.AWAITING_HUMAN.value
            _publish_state_changed(s, wfx)
            await s.commit()
            return

        # Local command — persist outcome + enqueue route_workflow.
        _persist_attempt(wfx, step_id, attempt)
        await enqueue(
            ROUTE_WORKFLOW,
            args={
                "workflow_execution_id": workflow_execution_id,
                "completed_step_id": step_id,
                "outcome_label": outcome.label,
                "outputs": _outcome_payload(outcome),
                "traceparent": traceparent,
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

    Span: nests under the upstream span from `traceparent` so the agent's
    work and the control-plane's resumption are one trace.
    """
    with with_remote_parent_span(_tracer, "workflow.handle_agent_event", traceparent) as span:
        span.set_attribute("workflow.execution_id", workflow_execution_id)
        span.set_attribute("workflow.agent_command_id", agent_command_id)
        span.set_attribute("workflow.outcome_label", outcome_label)
        await _handle_agent_event_impl(
            workflow_execution_id=workflow_execution_id,
            agent_command_id=agent_command_id,
            outcome_label=outcome_label,
            outputs=outputs,
            traceparent=traceparent,
        )


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
            log.info(
                "workflow.handle_agent_event.skip_state",
                workflow_execution_id=workflow_execution_id,
                state=wfx.state,
            )
            return

        if wfx.pending_agent_command_id is None or str(wfx.pending_agent_command_id) != agent_command_id:
            log.info(
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
                "traceparent": traceparent,
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

    Span: nests under the upstream span from `traceparent`.
    """
    with with_remote_parent_span(_tracer, "workflow.route_workflow", traceparent) as span:
        span.set_attribute("workflow.execution_id", workflow_execution_id)
        if completed_step_id is not None:
            span.set_attribute("workflow.completed_step_id", completed_step_id)
        if outcome_label is not None:
            span.set_attribute("workflow.outcome_label", outcome_label)
        await _route_workflow_impl(
            workflow_execution_id=workflow_execution_id,
            completed_step_id=completed_step_id,
            outcome_label=outcome_label,
            outputs=outputs,
            traceparent=traceparent,
        )


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
            log.info(
                "workflow.route_workflow.skip_terminal",
                workflow_execution_id=workflow_execution_id,
                state=wfx.state,
            )
            return

        if wfx.cancel_requested:
            await _enter_terminal_state(s, wfx, WorkflowState.CANCELLED)
            log.info(
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
            await _enqueue_start_step(s, wfx, wf, wf.entry_step_id, traceparent, attempt=0)
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
        # recovery command as an appended step that runs BEFORE the original
        # step retries. Recovery fires at most once per step instance — the
        # second hit falls through to Tier-2 retry / Tier-3 transition.
        if outcome_label and outcome_label != "success":
            recovery_kind = get_recovery_policy(outcome_label)
            if recovery_kind is not None and not _has_recovered(wfx, completed_step_id):
                _mark_recovered(wfx, completed_step_id, outcome_label)
                # Reset the failed step's attempt counter so the post-recovery
                # retry isn't already eating into the Tier-2 budget.
                _persist_attempt(wfx, completed_step_id, 0)
                from uuid import uuid4 as _uuid4  # noqa: PLC0415

                recovery_step = Step(id=f"_recover_{_uuid4().hex[:8]}", command_kind=recovery_kind)
                _queue_appended_steps(wfx, [recovery_step])
                _set_after_append(wfx, completed_step_id)
                wfx.state = WorkflowState.RUNNING.value
                _publish_state_changed(s, wfx)
                log.info(
                    "workflow.route_workflow.recovery_inserted",
                    workflow_execution_id=workflow_execution_id,
                    failed_step_id=completed_step_id,
                    failure_label=outcome_label,
                    recovery_kind=recovery_kind,
                )
                # Drain the appended step (the recovery) immediately.
                head = _dequeue_appended(wfx)
                assert head is not None  # we just queued it
                await _enqueue_start_step(s, wfx, wf, head.id, traceparent, attempt=0)
                await s.commit()
                return

        # Tier-2 retry on failure: bump attempt; re-enqueue start_step if
        # the budget allows. On exhaustion, fall through to the transition
        # map (Tier-3).
        if outcome_label == "failure":
            attempts = _get_attempt(wfx, completed_step_id)
            if attempts < step.retry_policy.max_attempts - 1:
                wfx.state = WorkflowState.RUNNING.value
                _publish_state_changed(s, wfx)
                await _enqueue_start_step(s, wfx, wf, completed_step_id, traceparent, attempt=attempts + 1)
                await s.commit()
                return

        # If the outcome included append_steps, queue them at the front and
        # snapshot the natural successor for after the chain is exhausted.
        append_steps_payload = outputs.get("__append_steps__") or []
        if append_steps_payload:
            natural_next = _resolve_transition(wf, step, outcome_label or "success")
            _queue_appended_steps(wfx, [Step.model_validate(d) for d in append_steps_payload])
            _set_after_append(wfx, natural_next)

        # Drain the append queue first — appended steps preempt static transitions.
        head = _dequeue_appended(wfx)
        if head is not None:
            wfx.state = WorkflowState.RUNNING.value
            _publish_state_changed(s, wfx)
            await _enqueue_start_step(s, wfx, wf, head.id, traceparent, attempt=0)
            await s.commit()
            return

        # Chain empty — if a post-chain destination was queued, take it.
        # Otherwise evaluate the completing step's static transition.
        target: str | TerminalAction
        after = _pop_after_append(wfx)
        if after is not None:
            target = after
        else:
            target = _resolve_transition(wf, step, outcome_label or "success")

        if target is TerminalAction.COMPLETE_WORKFLOW:
            # If the finalizer step already ran (i.e. we're completing the
            # cleanup step that ran after a prior failure), the workflow must
            # end as FAILED, not DONE.  The cleanup step's own transition
            # "success → COMPLETE_WORKFLOW" is the *normal-path* contract;
            # during the failure path the finalizer's completion is just the
            # resource-release gate before recording failure.
            if _has_finalizer_fired(wfx) and completed_step_id == wf.finalizer_step_id:
                target = TerminalAction.FAIL_WORKFLOW
            else:
                await _enter_terminal_state(s, wfx, WorkflowState.DONE)
                await s.commit()
                return
        if target is TerminalAction.FAIL_WORKFLOW:
            # Run the declared finalizer (one-shot) before recording failure.
            # The finalizer step runs cleanup (e.g. CleanupWorkspace) so it
            # has a chance to release resources even on the failure path.
            # Guard: fires only once per execution; if the finalizer itself
            # fails, it arrives here again with the fired flag set → skip.
            if wf.finalizer_step_id is not None and not _has_finalizer_fired(wfx):
                _mark_finalizer_fired(wfx)
                # Capture the failure context before routing to the finalizer;
                # the failure reason comes from the *original* failing step.
                failure_reason = _extract_failure_reason(outputs, outcome_label)
                _store_pending_failure(wfx, completed_step_id, failure_reason)
                wfx.state = WorkflowState.RUNNING.value
                _publish_state_changed(s, wfx)
                log.info(
                    "workflow.route_workflow.finalizer_dispatched",
                    workflow_execution_id=workflow_execution_id,
                    finalizer_step_id=wf.finalizer_step_id,
                    failed_step_id=completed_step_id,
                )
                await _enqueue_start_step(s, wfx, wf, wf.finalizer_step_id, traceparent, attempt=0)
                await s.commit()
                return

            # Finalizer already fired (or not declared) — record terminal failure.
            failure_reason = _pop_pending_failure_reason(wfx) or _extract_failure_reason(
                outputs, outcome_label
            )
            failed_step_id = _pop_pending_failure_step(wfx) or completed_step_id
            wfx.failure_reason = failure_reason
            await _enter_terminal_state(s, wfx, WorkflowState.FAILED)
            # Write the durable cross-cutting audit row.
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
        await _enqueue_start_step(s, wfx, wf, target, traceparent, attempt=0)
        await s.commit()


# Export the task refs.
START_STEP: TaskRef = start_step
HANDLE_AGENT_EVENT: TaskRef = handle_agent_event
ROUTE_WORKFLOW: TaskRef = route_workflow


# ── Admin APIs ──────────────────────────────────────────────────────────


async def request_cancel(workflow_execution_id: str, *, session: AsyncSession) -> bool:
    """Set the `cancel_requested` flag. The next `start_step` / `route_workflow`
    fire transitions the workflow to `cancelled`. If the workflow is
    `awaiting_agent`, the cancel takes effect after the terminal event
    arrives (per architecture.md § Cancellation interaction). Returns
    True if a row was updated."""
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
    """Resolve the open HITL decision for this workflow and enqueue the next
    routing step with `response` as the outcome's outputs. Caller commits.
    Returns True if a pending decision was resolved."""
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
    """Narrow read projection of a `workflow_executions` row. Consumers that
    need execution state for display or routing use this; they never receive
    the SQLAlchemy row directly."""

    id: UUID
    ticket_id: UUID
    workflow_name: str
    state: str
    current_step_id: str | None
    created_at: datetime
    updated_at: datetime
    # Set while the execution waits for an agent event (AWAITING_AGENT state).
    # Tests that need to seed a WorkspaceRow with the matching command_id read
    # this field rather than reaching into workflow_executions directly.
    pending_agent_command_id: UUID | None = None
    cancel_requested: bool = False
    # The OTel traceparent stored at engine.start() time. Exposed so callers
    # that simulate agent events (tests, agent_gateway) can propagate trace
    # context into handle_agent_event without reaching into the row directly.
    otel_trace_context: str | None = None
    # Short failure label written when the engine records terminal-fail.
    # None while the workflow is running or when it terminates with DONE.
    failure_reason: str | None = None


@dataclass(frozen=True)
class HitlHistoryEntry:
    """One HITL exchange (question + optional resolution). Projected from
    `pending_human_decisions` without exposing the SQLAlchemy row."""

    id: UUID
    workflow_execution_id: UUID
    question_payload: dict[str, Any]
    resolution_payload: dict[str, Any] | None
    resolved_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class WorkflowStepSummary:
    """One step within a workflow run, projected for the Ticket page UI.

    `state` is a five-valued projection — pending (not yet run) /
    running (current_step_id matches) / done / failed / skipped — derived
    from the workflow definition merged with `step_state[step_id]`. Pure
    workflow vocabulary — never references AgentCommands.
    """

    step_id: str
    command_kind: str
    state: str
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class WorkflowRunView:
    """One workflow execution rendered for the ticket page (steps + timing).

    The SPA renders one card per run plus a stage band built from `steps`.
    Steps are ordered by the workflow definition's `steps` tuple.
    """

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
    """Return all workflow executions for one ticket, newest first."""
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
    """Look up a single execution by id. Returns None when not found."""
    row = await session.get(WorkflowExecutionRow, execution_id)
    if row is None:
        return None
    return _project_execution(row)


async def get_awaiting_human_execution(
    ticket_id: UUID, *, session: AsyncSession
) -> WorkflowExecutionSummary | None:
    """Return the most recent `awaiting_human` execution for a ticket, or
    None if no execution is currently paused for HITL input."""
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
    """Return ids of all non-terminal executions for a ticket.

    Non-terminal: any state not in `TERMINAL_STATES`."""
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
    """All HITL exchanges for a ticket's executions, newest first."""
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
    """Parse an ISO-8601 string out of step_state. Returns None on any
    shape problem so a malformed stamp can't break the projection."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _step_summary(
    step: Step,
    *,
    is_current: bool,
    entry: dict[str, Any] | None,
    execution_state: str,
) -> WorkflowStepSummary:
    """Derive the projected step state from workflow def + stored entry.

    Branches: stored outcome_label (`success` → done, otherwise failed —
    `_skipped` is the conventional label for explicit skip); else current
    step → running; else default pending. On terminal `failed` / `cancelled`
    execution state, an entry-less step stays `pending` (never executed).
    """
    started = _parse_iso(entry.get("started_at")) if isinstance(entry, dict) else None
    completed = _parse_iso(entry.get("completed_at")) if isinstance(entry, dict) else None
    outcome_label: str | None = entry.get("outcome_label") if isinstance(entry, dict) else None

    if outcome_label == "success":
        state = "done"
    elif outcome_label is not None:
        # `_skipped` is the conventional label for an explicit skip outcome;
        # any other non-success label is a failure of the step itself.
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
        step_id=step.id,
        command_kind=step.command_kind,
        state=state,
        started_at=started,
        completed_at=completed,
    )


def _project_run_view(row: WorkflowExecutionRow) -> WorkflowRunView:
    """Merge workflow definition + execution row into a `WorkflowRunView`.

    Step order follows the workflow's declared `steps` tuple. Dynamically
    appended steps (via `Outcome.append_steps`) are not surfaced — the UI
    renders only the static pipeline. Unknown workflow names produce an
    empty step list rather than raising; the SPA still renders the run.
    """
    engine = get_engine()
    try:
        wf = engine.get_workflow(row.workflow_name, version=row.workflow_version)
    except WorkflowNotFoundError:
        steps: tuple[WorkflowStepSummary, ...] = ()
    else:
        summaries: list[WorkflowStepSummary] = []
        for step in wf.steps:
            entry = row.step_state.get(step.id)
            if not isinstance(entry, dict):
                entry = None
            summaries.append(
                _step_summary(
                    step,
                    is_current=(row.current_step_id == step.id),
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
    """All workflow runs for a ticket, newest first, fully projected.

    Each `WorkflowRunView` carries its step list (state + per-step timing)
    so the SPA can render the workflow-run card + stage band without a
    follow-up call. Ordering is `created_at DESC` (latest run first), matching
    the sibling `list_executions_for_ticket`.
    """
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
    """Return the `state` value for every execution row. Used for org-scoped
    metrics aggregation — callers group and count by state value."""
    return list((await session.execute(select(WorkflowExecutionRow.state))).scalars().all())


# ── Internal helpers ────────────────────────────────────────────────────


async def _load_execution(session: AsyncSession, workflow_execution_id: str) -> WorkflowExecutionRow | None:
    try:
        wid = UUID(workflow_execution_id)
    except TypeError, ValueError:
        return None
    return await session.get(WorkflowExecutionRow, wid)


def _resolve_step(wfx: WorkflowExecutionRow, wf: Workflow, step_id: str) -> Step | None:
    """Look up a step by id. Checks the appended-steps pool (filled by
    Outcome.append_steps) before falling back to the static workflow def.
    The pool is append-only for the lifetime of the workflow so a step
    that's been dequeued is still resolvable while it executes."""
    pool = wfx.step_state.get(_APPENDED_POOL_KEY, {})
    if step_id in pool:
        return Step.model_validate(pool[step_id])
    return wf.step_by_id(step_id)


def _resolve_transition(wf: Workflow, step: Step, outcome_label: str) -> str | TerminalAction:
    """Resolve the next target from the step's transition map. Defaults:
    success → next listed step (or `complete_workflow` if step is last);
    failure → `fail_workflow`."""
    explicit = step.transitions.get(outcome_label)
    if explicit is not None:
        return explicit
    if outcome_label == "success":
        # Next-in-list within the static workflow definition.
        ids = [s.id for s in wf.steps]
        if step.id in ids:
            idx = ids.index(step.id)
            if idx + 1 < len(ids):
                return ids[idx + 1]
        return TerminalAction.COMPLETE_WORKFLOW
    return TerminalAction.FAIL_WORKFLOW


async def _enqueue_start_step(
    session: AsyncSession,
    wfx: WorkflowExecutionRow,
    wf: Workflow,
    step_id: str,
    traceparent: str | None,
    *,
    attempt: int,
) -> None:
    """Resolve the step's `inputs` map against ticket payload + prior step
    outputs, then enqueue `workflow.start_step`."""
    step = _resolve_step(wfx, wf, step_id)
    resolved_inputs: dict[str, Any] = {}
    if step is not None:
        for input_name, source_expr in step.inputs.items():
            resolved_inputs[input_name] = _resolve_input_expression(wfx, source_expr)

    wfx.current_step_id = step_id

    await enqueue(
        START_STEP,
        args={
            "workflow_execution_id": str(wfx.id),
            "step_id": step_id,
            "attempt": attempt,
            "inputs": resolved_inputs,
            "traceparent": traceparent,
        },
        session=session,
    )


def _resolve_input_expression(wfx: WorkflowExecutionRow, expr: Any) -> Any:
    """Resolve workflow-input references. Anything not a `$`-prefixed string
    passes through verbatim.

    Supported shapes:
    - `$<step_id>.<field>` — resolves from prior step's `outputs`. Returns
      None if the step hasn't completed or the field isn't in outputs.
    - `$ticket.<field>` — resolves from the ticket payload stashed at
      `engine.start()` time (passed via the `ticket_payload` parameter,
      typically by the intake layer that already has it in hand). Returns
      None if `ticket_payload` wasn't supplied at start time or the field
      isn't in the payload.
    """
    if not isinstance(expr, str) or not expr.startswith("$"):
        return expr
    body = expr[1:]
    if "." not in body:
        return None
    head, tail = body.split(".", 1)
    if head == "ticket":
        payload = wfx.step_state.get(_TICKET_PAYLOAD_KEY)
        if not isinstance(payload, dict):
            return None
        return payload.get(tail)
    bucket = wfx.step_state.get(head)
    if not isinstance(bucket, dict):
        return None
    return bucket.get("outputs", {}).get(tail)


def _has_recovered(wfx: WorkflowExecutionRow, step_id: str) -> bool:
    """Has recovery already been inserted for this step instance? One-shot
    per step to prevent infinite auth-refresh loops."""
    return step_id in wfx.step_state.get(_RECOVERED_STEPS_KEY, {})


def _mark_recovered(wfx: WorkflowExecutionRow, step_id: str, failure_label: str) -> None:
    """Mark this step as having had recovery applied (with the triggering
    label) so a second attempt at recovery falls through to Tier-2 retry."""
    bucket = dict(wfx.step_state)
    recovered = dict(bucket.get(_RECOVERED_STEPS_KEY, {}))
    recovered[step_id] = failure_label
    bucket[_RECOVERED_STEPS_KEY] = recovered
    wfx.step_state = bucket


def _persist_attempt(wfx: WorkflowExecutionRow, step_id: str, attempt: int) -> None:
    """Reassigns the whole `step_state` dict so SQLAlchemy detects the
    change — JSONB columns don't track in-place mutation."""
    bucket = dict(wfx.step_state)
    attempts = dict(bucket.get(_ATTEMPTS_KEY, {}))
    attempts[step_id] = attempt
    bucket[_ATTEMPTS_KEY] = attempts
    wfx.step_state = bucket


def _get_attempt(wfx: WorkflowExecutionRow, step_id: str) -> int:
    attempts = wfx.step_state.get(_ATTEMPTS_KEY, {})
    return int(attempts.get(step_id, 0))


def _dequeue_appended(wfx: WorkflowExecutionRow) -> Step | None:
    """Pop the head id off the execution queue and return the corresponding
    Step from the pool. The pool retains the definition so the in-flight
    step can still be resolved by id after dequeueing."""
    queue = list(wfx.step_state.get(_APPEND_QUEUE_KEY, []))
    if not queue:
        return None
    head_id = queue.pop(0)
    bucket = dict(wfx.step_state)
    bucket[_APPEND_QUEUE_KEY] = queue
    wfx.step_state = bucket
    pool = wfx.step_state.get(_APPENDED_POOL_KEY, {})
    raw = pool.get(head_id)
    if raw is None:
        return None
    return Step.model_validate(raw)


def _set_after_append(wfx: WorkflowExecutionRow, target: str | TerminalAction | None) -> None:
    """Persist the post-append-chain destination. Stored as a string id or
    a TerminalAction value; cleared by assignment to None."""
    bucket = dict(wfx.step_state)
    if target is None:
        bucket.pop(_AFTER_APPEND_KEY, None)
    elif isinstance(target, TerminalAction):
        bucket[_AFTER_APPEND_KEY] = {"terminal": target.value}
    else:
        bucket[_AFTER_APPEND_KEY] = {"step_id": target}
    wfx.step_state = bucket


def _pop_after_append(
    wfx: WorkflowExecutionRow,
) -> str | TerminalAction | None:
    raw = wfx.step_state.get(_AFTER_APPEND_KEY)
    if raw is None:
        return None
    bucket = dict(wfx.step_state)
    bucket.pop(_AFTER_APPEND_KEY, None)
    wfx.step_state = bucket
    if "terminal" in raw:
        return TerminalAction(raw["terminal"])
    return raw["step_id"]


def _persist_outputs(
    wfx: WorkflowExecutionRow,
    step_id: str,
    outcome_label: str | None,
    outputs: dict[str, Any],
) -> None:
    bucket = dict(wfx.step_state)
    # Preserve `started_at` if already stamped by `_stamp_step_started`.
    prior = bucket.get(step_id) if isinstance(bucket.get(step_id), dict) else None
    started_at = prior.get("started_at") if isinstance(prior, dict) else None
    # Strip control keys before persisting per-step entries — those control
    # keys live alongside step entries with their own '__' prefixes.
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
    """Stamp `started_at` on the step's entry if not already present.

    Called at dispatch time so a still-running step has `started_at` but a
    null `completed_at` for the run-view UI. `_persist_outputs` later
    preserves the stamp when writing the terminal outcome.
    """
    bucket = dict(wfx.step_state)
    existing = bucket.get(step_id) if isinstance(bucket.get(step_id), dict) else None
    if isinstance(existing, dict) and "started_at" in existing:
        return
    entry: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    entry["started_at"] = datetime.now(_UTC).isoformat()
    bucket[step_id] = entry
    wfx.step_state = bucket


def _publish_state_changed(session: AsyncSession, wfx: WorkflowExecutionRow) -> None:
    """Emit `workflow_state_changed` on the org's general SSE channel.

    Called at every `wfx.state = …` assignment site so the SPA can keep the
    stage band + Activity tab live. Stashed after-commit so a rolled-back
    transition never reaches subscribers. core→core only — the engine
    never imports `domain/*`.
    """
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
    """Set the execution to a terminal state, publish the SSE event, and
    await all registered terminal hooks inside the current transaction.

    All terminal transitions in the engine funnel through here so hooks
    are guaranteed to fire exactly once per terminal write. Hooks run in
    registration order. A raising hook rolls back the transaction — the
    caller must not commit before this returns.
    """
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


def _queue_appended_steps(wfx: WorkflowExecutionRow, steps: list[Step]) -> None:
    """Register the appended step defs in the persistent pool AND prepend
    their ids to the execution queue."""
    bucket = dict(wfx.step_state)
    pool = dict(bucket.get(_APPENDED_POOL_KEY, {}))
    for s in steps:
        pool[s.id] = s.model_dump(mode="json")
    bucket[_APPENDED_POOL_KEY] = pool
    queue = list(bucket.get(_APPEND_QUEUE_KEY, []))
    queue = [s.id for s in steps] + queue
    bucket[_APPEND_QUEUE_KEY] = queue
    wfx.step_state = bucket


def _outcome_payload(outcome: Outcome) -> dict[str, Any]:
    """Pack an Outcome's outputs + append_steps into the routing task's
    `outputs` argument."""
    payload: dict[str, Any] = dict(outcome.outputs)
    if outcome.append_steps:
        payload["__append_steps__"] = [s.model_dump(mode="json") for s in outcome.append_steps]
    if outcome.failure_reason is not None:
        payload["__failure_reason__"] = outcome.failure_reason
    return payload


# ── Finalizer helpers ────────────────────────────────────────────────────
# The finalizer is a one-shot per execution: once dispatched it is never
# re-dispatched even if it itself fails. The fired-flag + pending-failure
# cache live in step_state under control keys so SQLAlchemy detects changes.

_PENDING_FAILURE_STEP_KEY = "__pending_failure_step__"
_PENDING_FAILURE_REASON_KEY = "__pending_failure_reason__"


def _has_finalizer_fired(wfx: WorkflowExecutionRow) -> bool:
    return bool(wfx.step_state.get(_FINALIZER_FIRED_KEY))


def _mark_finalizer_fired(wfx: WorkflowExecutionRow) -> None:
    bucket = dict(wfx.step_state)
    bucket[_FINALIZER_FIRED_KEY] = True
    wfx.step_state = bucket


def _store_pending_failure(
    wfx: WorkflowExecutionRow,
    failed_step_id: str | None,
    failure_reason: str | None,
) -> None:
    """Stash the failure context so it survives the finalizer step's round-trip
    and can be read when the finalizer itself completes (or fails)."""
    bucket = dict(wfx.step_state)
    if failed_step_id is not None:
        bucket[_PENDING_FAILURE_STEP_KEY] = failed_step_id
    if failure_reason is not None:
        bucket[_PENDING_FAILURE_REASON_KEY] = failure_reason
    wfx.step_state = bucket


def _pop_pending_failure_step(wfx: WorkflowExecutionRow) -> str | None:
    raw = wfx.step_state.get(_PENDING_FAILURE_STEP_KEY)
    if raw is None:
        return None
    bucket = dict(wfx.step_state)
    bucket.pop(_PENDING_FAILURE_STEP_KEY, None)
    wfx.step_state = bucket
    return str(raw)


def _pop_pending_failure_reason(wfx: WorkflowExecutionRow) -> str | None:
    raw = wfx.step_state.get(_PENDING_FAILURE_REASON_KEY)
    if raw is None:
        return None
    bucket = dict(wfx.step_state)
    bucket.pop(_PENDING_FAILURE_REASON_KEY, None)
    wfx.step_state = bucket
    return str(raw)


def _extract_failure_reason(outputs: dict[str, Any], outcome_label: str | None) -> str | None:
    """Best-effort failure-reason extraction. Prefers the structured
    `__failure_reason__` output key set by `Outcome.failure(reason=...)`;
    falls back to the outcome label string."""
    reason = outputs.get("__failure_reason__")
    if reason:
        return str(reason)
    if outcome_label and outcome_label != "success":
        return outcome_label
    return None


def _workflow_org_id(wfx: WorkflowExecutionRow) -> UUID:
    """Return the org_id for the audit call.

    Route-workflow task bodies run inside `OrgContextMiddleware` which sets
    the `org_id` contextvar from the task metadata. Use that when available;
    fall back to the ticket_payload stash for test cases that bypass the
    middleware.
    """
    from app.core.auth import current_org_id  # noqa: PLC0415

    org_id = current_org_id()
    if org_id is not None:
        return org_id
    # Fallback: ticket_payload may carry org_id for tests that bypass middleware.
    payload = wfx.step_state.get(_TICKET_PAYLOAD_KEY)
    if isinstance(payload, dict):
        raw = payload.get("org_id")
        if raw is not None:
            return UUID(str(raw))
    # Last resort — mint a nil UUID so the audit row still lands rather than
    # crashing the failure path; the engine logs the issue.
    log.warning(
        "workflow.route_workflow.no_org_id_for_audit",
        workflow_execution_id=str(wfx.id),
    )
    return UUID(int=0)


async def _safe_execute(
    command: WorkflowCommand,
    inputs: dict[str, Any],
    ctx: CommandContext,
) -> Outcome:
    """Call command.execute(inputs, ctx). Any exception becomes a
    failure outcome — commands shouldn't raise, but a defensive catch
    keeps engine state consistent."""
    try:
        # Pass raw inputs through; commands are responsible for parsing
        # their typed inputs from the raw dict via Pydantic if they care.
        return await command.execute(inputs, ctx)  # type: ignore[arg-type]
    except Exception as exc:
        log.exception(
            "workflow.command.raised",
            workflow_execution_id=ctx.workflow_execution_id,
            step_id=ctx.step_id,
        )
        return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")


# ── Engine ──────────────────────────────────────────────────────────────


class WorkflowEngine:
    """Workflow + WorkflowCommand registry. Process-singleton via
    `get_engine()`. Domain modules call `register_workflow(...)` and
    `register_command(...)` once at import time.

    `start(workflow_name, ticket_id, *, session)` opens a workflow execution
    and enqueues the initial routing task. Required `session` — the caller
    commits and the outbox drain delivers the task post-commit.
    """

    def __init__(self) -> None:
        self._workflows: dict[tuple[str, int], Workflow] = {}
        self._commands: dict[str, WorkflowCommand] = {}

    def register_workflow(self, wf: Workflow) -> None:
        key = (wf.name, wf.version)
        if key in self._workflows:
            raise WorkflowError(f"workflow '{wf.name}' v{wf.version} already registered")
        if wf.step_by_id(wf.entry_step_id) is None:
            raise WorkflowError(f"workflow '{wf.name}' entry_step_id '{wf.entry_step_id}' not in steps")
        for step in wf.steps:
            for label, target in step.transitions.items():
                # TerminalAction subclasses str — exclude before resolving as step id.
                if isinstance(target, TerminalAction):
                    continue
                if isinstance(target, str) and wf.step_by_id(target) is None:
                    raise WorkflowError(
                        f"workflow '{wf.name}' step '{step.id}' transitions['{label}'] points to "
                        f"unknown step '{target}'"
                    )
        self._workflows[key] = wf

    def register_command(self, command: WorkflowCommand) -> None:
        if command.kind in self._commands:
            raise WorkflowError(f"WorkflowCommand kind '{command.kind}' already registered")
        self._commands[command.kind] = command

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
        ticket_payload: dict[str, Any] | None = None,
        session: AsyncSession,
    ) -> str:
        """Create a `workflow_executions` row in `pending` state, enqueue
        the initial `route_workflow` task (which decides the first step),
        and return the new execution id. Required `session` — the caller
        commits and the outbox drain delivers the task post-commit.

        `ticket_payload`: the ticket's intake payload dict. Stashed on the
        execution row so workflow input expressions can reference
        `$ticket.<field>` (e.g. `$ticket.head_sha`) without each step
        re-fetching from the DB. Callers that have it in hand (intake
        layer) pass it; callers without can omit — the resolver returns
        None for missing fields.

        Workspace commands always dispatch over the wire to the registered
        WorkspaceProvider and park in `awaiting_agent`. Provider resolution
        and errors surface in the workspace module's actual dispatch, not
        here."""
        wf = self.get_workflow(workflow_name, version=version)
        for step in wf.steps:
            self.get_command(step.command_kind)

        initial_state: dict[str, Any] = {}
        if ticket_payload is not None:
            initial_state[_TICKET_PAYLOAD_KEY] = dict(ticket_payload)

        row = WorkflowExecutionRow(
            ticket_id=ticket_id,
            workflow_name=wf.name,
            workflow_version=wf.version,
            state=WorkflowState.PENDING.value,
            current_step_id=None,
            pending_agent_command_id=None,
            step_state=initial_state,
            cancel_requested=False,
            otel_trace_context=traceparent,
        )
        session.add(row)
        await session.flush()

        await enqueue(
            ROUTE_WORKFLOW,
            args={
                "workflow_execution_id": str(row.id),
                "completed_step_id": None,
                "outcome_label": None,
                "outputs": {},
                "traceparent": traceparent,
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
    """Swap the process-singleton engine and return the prior instance.

    The test harness (`app.testing.workflow_harness.scoped_engine`) is the
    intended caller. Production code reads the engine via `get_engine()` and
    never calls this function.
    """
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
