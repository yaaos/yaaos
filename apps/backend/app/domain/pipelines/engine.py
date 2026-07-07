"""The run engine — `ROUTE_RUN` + `START_STAGE` taskiq task bodies driving
one `PipelineRun` end to end, mirroring the `route_workflow`/`start_step`
trio in `apps/backend/app/core/workflow/service.py:540,113`: outbox-atomic
enqueue, SAVEPOINT-wrapped command execution, exception→failure mapping.

Only `action` stages dispatch here. Skill/review stage execution needs the
coding-agent invocation wiring (`core/coding_agent.dispatch_invocation`
threaded with an engine-minted `command_id`) that doesn't exist yet in this
engine — a run that reaches a skill/review stage fails loudly with a named
reason rather than hanging. Because of that, run `phase` never leaves
`'stages'` here: workspace provision/cleanup are `kind='system'` stage
executions the skill-stage machinery creates, and an action-only pipeline
has nothing for them to do — the same "zero-skill pipelines skip workspace
work" behavior is permanent, not just true this phase.

Every run in `queued` is promoted to `running` by `attempt_promotion`,
guarded by the `ux_pipeline_runs_one_in_flight` partial unique index so a
race between two promotion attempts for the same ticket can never leave two
runs `running` at once — the loser's UPDATE hits the unique violation and
the run stays `queued`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit
from app.core.database import session as db_session
from app.core.notifications import create as create_notification
from app.core.sse import GeneralEventKind, publish_general_after_commit
from app.core.tasks import TaskRef, enqueue, task
from app.domain.actions import ActionContext, ActionError, get_action
from app.domain.pipelines.definition import ActionStage, FlattenedDefinition
from app.domain.pipelines.escalation import resolve_escalation_targets
from app.domain.pipelines.models import PipelineRunRow, StageExecutionRow
from app.domain.pipelines.types import Kickoff
from app.domain.tickets import (
    get as get_ticket,
)
from app.domain.tickets import (
    get_pull_request,
    set_current_run,
    transition_ticket_on_run_start,
    transition_ticket_on_run_terminal,
)

log = structlog.get_logger("domain.pipelines.engine")

_RUN_STATE_TO_TICKET_STATUS = {
    "completed": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "killed": "cancelled",
}


class _RunStartedPayload(BaseModel):
    ticket_id: str
    pipeline_name: str


class _RunTerminalPayload(BaseModel):
    ticket_id: str
    pipeline_name: str
    failure_reason: str | None


def _publish_run_state(session: AsyncSession, run: PipelineRunRow) -> None:
    publish_general_after_commit(
        session,
        org_id=run.org_id,
        kind=GeneralEventKind.RUN_STATE_CHANGED,
        payload={"ticket_id": str(run.ticket_id), "run_id": str(run.id), "state": run.state},
    )


# ---------------------------------------------------------------------------
# Promotion — queued -> running, guarded by the one-in-flight unique index
# ---------------------------------------------------------------------------


async def attempt_promotion(run: PipelineRunRow, *, session: AsyncSession) -> bool:
    """Try to flip `run` `queued -> running`. Returns True iff promoted.

    Guarded by `ux_pipeline_runs_one_in_flight`: the UPDATE below is wrapped
    in a SAVEPOINT so a unique-violation (another run on the same ticket is
    already running/paused) rolls back just this attempt, leaving `run`
    queued — never the outer caller's transaction.
    """
    if run.state != "queued":
        return False
    try:
        async with session.begin_nested():
            result = await session.execute(
                update(PipelineRunRow)
                .where(PipelineRunRow.id == run.id, PipelineRunRow.state == "queued")
                .values(state="running")
            )
    except IntegrityError:
        return False
    if result.rowcount != 1:
        return False

    run.state = "running"
    kickoff = Kickoff.model_validate(run.kickoff)
    await set_current_run(run.ticket_id, run.id, session=session)
    await transition_ticket_on_run_start(run.ticket_id, org_id=run.org_id, run_id=run.id, session=session)
    await audit(
        "pipeline_run",
        run.id,
        "run.started",
        _RunStartedPayload(ticket_id=str(run.ticket_id), pipeline_name=run.pipeline_name),
        actor=kickoff.actor,
        org_id=run.org_id,
        session=session,
    )
    _publish_run_state(session, run)
    await enqueue(
        ROUTE_RUN,
        args={
            "run_id": str(run.id),
            "completed_stage_index": None,
            "outcome_label": None,
            "failure_reason": None,
        },
        session=session,
    )
    return True


async def promote_oldest_queued(ticket_id: UUID, *, session: AsyncSession) -> None:
    """Promote the oldest `queued` run on `ticket_id` (uuid7 order — `id`
    sorts chronologically), if any. Called after every run reaches a
    terminal state so a queued sibling never waits forever."""
    candidate = (
        await session.execute(
            select(PipelineRunRow)
            .where(PipelineRunRow.ticket_id == ticket_id, PipelineRunRow.state == "queued")
            .order_by(PipelineRunRow.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if candidate is not None:
        await attempt_promotion(candidate, session=session)


async def cancel_queued(run: PipelineRunRow, *, actor: Actor, session: AsyncSession) -> None:
    """Cancel a `queued` run directly — it never occupied the one-in-flight
    slot, so no promotion sweep is needed."""
    run.state = "cancelled"
    run.completed_at = datetime.now(UTC)
    _publish_run_state(session, run)
    await audit(
        "pipeline_run",
        run.id,
        "run.cancelled",
        _RunTerminalPayload(
            ticket_id=str(run.ticket_id), pipeline_name=run.pipeline_name, failure_reason=None
        ),
        actor=actor,
        org_id=run.org_id,
        session=session,
    )


# ---------------------------------------------------------------------------
# ActionContext assembly
# ---------------------------------------------------------------------------


async def _build_action_context(run: PipelineRunRow, kickoff: Kickoff, *, org_id: UUID) -> ActionContext:
    ticket = await get_ticket(run.ticket_id, org_id=org_id)
    pr_external_id: str | None = None
    if ticket.pr_id is not None:
        pr = await get_pull_request(ticket.pr_id, org_id=org_id)
        pr_external_id = pr.external_id
    return ActionContext(
        org_id=org_id,
        ticket_id=run.ticket_id,
        run_id=run.id,
        repo_external_id=ticket.repo_external_id,
        vcs_plugin_id=ticket.plugin_id,
        pr_external_id=pr_external_id,
        branch_name=ticket.branch_name or "",
        intake_point_id=kickoff.intake_point_id,
        kickoff_input=kickoff.input_text,
        # No skill/review stage dispatch exists yet in this engine, so no
        # stage ever precedes an action with residual findings/verdicts/an
        # artifact to hand it — these stay empty until that dispatch lands.
        preceding_residuals=(),
        preceding_verdicts=(),
        preceding_artifact_id=None,
    )


# ---------------------------------------------------------------------------
# ROUTE_RUN — decide the next boundary action
# ---------------------------------------------------------------------------


@task("pipelines.route_run", queue="pipelines", max_retries=1)
async def route_run(
    *,
    run_id: str,
    completed_stage_index: int | None,
    outcome_label: str | None,
    failure_reason: str | None = None,
    traceparent: str | None = None,
) -> None:
    del traceparent  # reserved for span-reparenting once spans are added here
    async with db_session() as s:
        await _route_run_impl(
            run_id=UUID(run_id),
            completed_stage_index=completed_stage_index,
            outcome_label=outcome_label,
            failure_reason=failure_reason,
            session=s,
        )
        await s.commit()


async def _route_run_impl(
    *,
    run_id: UUID,
    completed_stage_index: int | None,
    outcome_label: str | None,
    failure_reason: str | None,
    session: AsyncSession,
) -> None:
    run = await session.get(PipelineRunRow, run_id)
    if run is None:
        log.warning("pipelines.route_run.unknown_run", run_id=str(run_id))
        return
    if run.state != "running":
        log.debug("pipelines.route_run.skip_not_running", run_id=str(run_id), state=run.state)
        return

    flattened = FlattenedDefinition.from_snapshot(run.definition_snapshot)
    total_stages = len(flattened.stages)

    if completed_stage_index is None:
        # Bootstrap call from `attempt_promotion` — kick off the first stage.
        await _dispatch_stage(run, stage_index=0, session=session)
        return

    if outcome_label == "failure":
        await _enter_terminal(run, "failed", failure_reason=failure_reason, session=session)
        return

    # Boundary evaluation is a labeled stub that always proceeds — real
    # conditional/HITL boundary evaluation doesn't exist yet. Cancel is
    # checked at every boundary regardless, including the last one.
    if run.cancel_requested:
        await _enter_terminal(run, "cancelled", failure_reason=None, session=session)
        return

    next_index = completed_stage_index + 1
    if next_index >= total_stages:
        await _enter_terminal(run, "completed", failure_reason=None, session=session)
        return

    await _dispatch_stage(run, stage_index=next_index, session=session)


async def _dispatch_stage(run: PipelineRunRow, *, stage_index: int, session: AsyncSession) -> None:
    run.current_stage_index = stage_index
    await enqueue(START_STAGE, args={"run_id": str(run.id), "stage_index": stage_index}, session=session)


async def _enter_terminal(
    run: PipelineRunRow, state: str, *, failure_reason: str | None, session: AsyncSession
) -> None:
    run.state = state
    run.failure_reason = failure_reason
    run.completed_at = datetime.now(UTC)
    _publish_run_state(session, run)

    to_status = _RUN_STATE_TO_TICKET_STATUS[state]
    await transition_ticket_on_run_terminal(
        run.ticket_id,
        org_id=run.org_id,
        run_id=run.id,
        to_status=to_status,  # type: ignore[arg-type]
        reason=failure_reason,
        session=session,
    )

    await audit(
        "pipeline_run",
        run.id,
        f"run.{state}",
        _RunTerminalPayload(
            ticket_id=str(run.ticket_id), pipeline_name=run.pipeline_name, failure_reason=failure_reason
        ),
        actor=Actor.system(),
        org_id=run.org_id,
        session=session,
    )
    log.info("pipelines.run.terminal", run_id=str(run.id), state=state, failure_reason=failure_reason)

    if state == "failed":
        kickoff = Kickoff.model_validate(run.kickoff)
        targets = await resolve_escalation_targets(kickoff, run.org_id, session=session)
        for user_id in targets:
            await create_notification(
                user_id=user_id,
                org_id=run.org_id,
                type="pipeline_run_failed",
                title=f"{run.pipeline_name} failed",
                body=failure_reason or "Run failed.",
                subject_type="pipeline_run",
                subject_id=run.id,
                session=session,
            )

    await promote_oldest_queued(run.ticket_id, session=session)


# ---------------------------------------------------------------------------
# START_STAGE — execute one stage
# ---------------------------------------------------------------------------


@task("pipelines.start_stage", queue="pipelines", max_retries=1)
async def start_stage(*, run_id: str, stage_index: int, traceparent: str | None = None) -> None:
    del traceparent
    async with db_session() as s:
        await _start_stage_impl(run_id=UUID(run_id), stage_index=stage_index, session=s)
        await s.commit()


async def _start_stage_impl(*, run_id: UUID, stage_index: int, session: AsyncSession) -> None:
    run = await session.get(PipelineRunRow, run_id)
    if run is None or run.state != "running":
        log.debug("pipelines.start_stage.skip_not_running", run_id=str(run_id))
        return

    flattened = FlattenedDefinition.from_snapshot(run.definition_snapshot)
    stage = flattened.stages[stage_index]
    kickoff = Kickoff.model_validate(run.kickoff)

    if isinstance(stage, ActionStage):
        await _run_action_stage(run, stage, stage_index=stage_index, kickoff=kickoff, session=session)
        return

    # Skill/review stage dispatch needs the coding-agent invocation wiring —
    # not built here. Fail the run loudly with a stage_executions row rather
    # than hang forever awaiting an agent command that will never be sent.
    stage_exec = StageExecutionRow(
        org_id=run.org_id,
        run_id=run.id,
        stage_index=stage_index,
        kind=stage.kind,
        stage_name=stage.name,
        skill_name=stage.skill_name,
        status="failed",
        failure_reason="skill stage dispatch is not implemented in this engine yet",
        completed_at=datetime.now(UTC),
    )
    session.add(stage_exec)
    await enqueue(
        ROUTE_RUN,
        args={
            "run_id": str(run.id),
            "completed_stage_index": stage_index,
            "outcome_label": "failure",
            "failure_reason": stage_exec.failure_reason,
        },
        session=session,
    )


async def _run_action_stage(
    run: PipelineRunRow,
    stage: ActionStage,
    *,
    stage_index: int,
    kickoff: Kickoff,
    session: AsyncSession,
) -> None:
    stage_exec = StageExecutionRow(
        org_id=run.org_id,
        run_id=run.id,
        stage_index=stage_index,
        kind="action",
        stage_name=stage.action_id,
        status="running",
    )
    session.add(stage_exec)
    await session.flush()

    ctx = await _build_action_context(run, kickoff, org_id=run.org_id)

    failure_reason: str | None = None
    try:
        async with session.begin_nested():
            action = get_action(stage.action_id)
            result = await action.execute(ctx, session=session)
    except ActionError as exc:
        failure_reason = str(exc)
    else:
        stage_exec.status = "completed"
        stage_exec.action_result = result.model_dump(mode="json")
        stage_exec.boundary_outcome = "proceeded"
        stage_exec.completed_at = datetime.now(UTC)
        await enqueue(
            ROUTE_RUN,
            args={
                "run_id": str(run.id),
                "completed_stage_index": stage_index,
                "outcome_label": "success",
                "failure_reason": None,
            },
            session=session,
        )
        return

    # ActionError path: the SAVEPOINT rolled back the action's own writes;
    # refresh stage_exec (session-wide expiry on nested rollback) before
    # writing the failure onto it — same idiom as
    # `core/workflow/service.py`'s LocalCommand exception handling.
    await session.refresh(stage_exec)
    stage_exec.status = "failed"
    stage_exec.failure_reason = failure_reason
    stage_exec.completed_at = datetime.now(UTC)
    await enqueue(
        ROUTE_RUN,
        args={
            "run_id": str(run.id),
            "completed_stage_index": stage_index,
            "outcome_label": "failure",
            "failure_reason": failure_reason,
        },
        session=session,
    )


# Export the task refs.
ROUTE_RUN: TaskRef = route_run
START_STAGE: TaskRef = start_stage
