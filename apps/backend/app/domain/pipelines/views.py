"""Read models for `domain/pipelines` — the ticket-page Runs tab
(`list_runs_for_ticket`) and Overview tab (`get_run_overview`).

Both build `types.py` VOs straight off `pipeline_runs` / `stage_executions`
/ `run_pauses` rows — no separate query-side table, no caching. Per-stage
`artifact_ids` come from `stage_executions.loop_state` (every produced
artifact is already referenced there by the engine) rather than a fresh
`domain/artifacts` query, since `loop_state` is the durable per-attempt
record and cross-module reads would need a new `domain/artifacts` API this
phase doesn't otherwise require.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, ActorKind
from app.core.auth import require_org_context
from app.core.sessions import current_actor
from app.core.tenancy import get_membership_info
from app.domain.findings import list_for_stage_execution
from app.domain.pipelines.escalation import is_pause_responder
from app.domain.pipelines.models import PipelineRunRow, RunPauseRow, StageExecutionRow
from app.domain.pipelines.types import (
    Decision,
    Kickoff,
    PauseDetail,
    PipelineRun,
    RunKickoffView,
    RunOutcome,
    RunOverview,
    StageExecution,
)
from app.domain.tickets import get as get_ticket

_ACTIVE_RUN_STATES = frozenset({"queued", "running"})


async def _login_for(user_id: UUID | None, *, org_id: UUID, session: AsyncSession) -> str | None:
    if user_id is None:
        return None
    info = await get_membership_info(session, user_id=user_id, org_id=org_id)
    return info.handle if info is not None else None


async def _actor_login(actor: Actor, *, org_id: UUID, session: AsyncSession) -> str | None:
    if actor.login:
        return actor.login
    if actor.kind == ActorKind.USER and actor.user_id is not None:
        return await _login_for(actor.user_id, org_id=org_id, session=session)
    return None


def _artifact_ids_from_loop_state(loop_state: list[dict]) -> tuple[UUID, ...]:
    """Every `artifact_id` an engine loop-state entry recorded, in the order
    produced (main, then each fix pass)."""
    return tuple(UUID(entry["artifact_id"]) for entry in loop_state if entry.get("artifact_id") is not None)


async def _build_pipeline_run(run: PipelineRunRow, *, session: AsyncSession) -> PipelineRun:
    kickoff = Kickoff.model_validate(run.kickoff)
    stage_rows = (
        (
            await session.execute(
                select(StageExecutionRow)
                .where(StageExecutionRow.run_id == run.id)
                # `id` breaks the tie: rows inserted in one transaction share
                # `started_at` (Postgres `now()` is transaction-start time),
                # and uuid7 ids are monotonic, so the pair is a total order.
                .order_by(StageExecutionRow.started_at, StageExecutionRow.id)
            )
        )
        .scalars()
        .all()
    )
    pause_rows = (
        (
            await session.execute(
                select(RunPauseRow).where(RunPauseRow.run_id == run.id, RunPauseRow.resolved_at.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    decisions_by_stage_exec: dict[UUID, list[Decision]] = {}
    for pause in pause_rows:
        assert pause.resolved_at is not None
        decisions_by_stage_exec.setdefault(pause.stage_execution_id, []).append(
            Decision(
                action=pause.resolution or "",
                actor_login=await _login_for(pause.resolved_by, org_id=run.org_id, session=session),
                instruction=pause.instruction,
                resolved_at=pause.resolved_at,
            )
        )

    stages = tuple(
        StageExecution(
            id=row.id,
            stage_index=row.stage_index,
            kind=row.kind,  # type: ignore[arg-type]
            stage_name=row.stage_name,
            status=row.status,
            confidence=row.confidence,  # type: ignore[arg-type]
            review_iterations=row.iteration,
            boundary_outcome=row.boundary_outcome,  # type: ignore[arg-type]
            artifact_ids=_artifact_ids_from_loop_state(row.loop_state),
            action_result=row.action_result,
            decisions=tuple(decisions_by_stage_exec.get(row.id, [])),
            failure_reason=row.failure_reason,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
        for row in stage_rows
    )

    return PipelineRun(
        id=run.id,
        pipeline_name=run.pipeline_name,
        state=run.state,  # type: ignore[arg-type]
        kickoff=RunKickoffView(
            intake_point_id=kickoff.intake_point_id,
            actor_kind=kickoff.actor.kind.value,
            actor_login=await _actor_login(kickoff.actor, org_id=run.org_id, session=session),
            input_text=kickoff.input_text,
        ),
        created_at=run.created_at,
        completed_at=run.completed_at,
        failure_reason=run.failure_reason,
        stages=stages,
    )


async def list_runs_for_ticket(ticket_id: UUID, *, session: AsyncSession) -> list[PipelineRun]:
    """Runs-tab timeline — newest first (uuid7 `id` sorts chronologically);
    no pagination (tickets hold few runs — do not add a `limit` param)."""
    org_id = require_org_context()
    rows = (
        (
            await session.execute(
                select(PipelineRunRow)
                .where(PipelineRunRow.ticket_id == ticket_id, PipelineRunRow.org_id == org_id)
                .order_by(PipelineRunRow.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [await _build_pipeline_run(row, session=session) for row in rows]


async def get_run_overview(ticket_id: UUID, *, session: AsyncSession) -> RunOverview | None:
    """Overview-tab payload for the ticket's current run (`tickets.current_run_id`
    — set at every promotion attempt, never cleared at terminal). `None`
    when the ticket has no run yet. `can_respond` reflects the CALLING
    request's actor (`current_actor()` — the session contextvar `require(...)`
    already set), not a context-free fact — matches the escalation-set-union-
    admins rule `service.resolve_pause` enforces."""
    org_id = require_org_context()
    try:
        ticket = await get_ticket(ticket_id, org_id=org_id)
    except LookupError:
        return None
    if ticket.current_run_id is None:
        return None
    run = await session.get(PipelineRunRow, ticket.current_run_id)
    if run is None:
        return None

    if run.state == "paused":
        pause = (
            await session.execute(
                select(RunPauseRow).where(RunPauseRow.run_id == run.id, RunPauseRow.resolved_at.is_(None))
            )
        ).scalar_one()
        stage_exec = await session.get(StageExecutionRow, pause.stage_execution_id)
        assert stage_exec is not None
        residuals = tuple(
            f for f in await list_for_stage_execution(stage_exec.id, session=session) if f.status == "open"
        )
        artifact_ids = _artifact_ids_from_loop_state(stage_exec.loop_state)
        escalation_logins = tuple(
            login
            for login in [
                await _login_for(user_id, org_id=org_id, session=session)
                for user_id in pause.escalation_user_ids
            ]
            if login is not None
        )
        actor = current_actor()
        can_respond = actor.user_id is not None and await is_pause_responder(
            actor.user_id, pause.escalation_user_ids, org_id=org_id, session=session
        )
        return RunOverview(
            status="paused",
            pause=PauseDetail(
                pause_id=pause.id,
                stage_name=stage_exec.stage_name,
                tripped=pause.tripped,
                artifact_id=artifact_ids[-1] if artifact_ids else None,
                residuals=residuals,
                escalation_logins=escalation_logins,
                can_respond=can_respond,
            ),
        )

    if run.state in _ACTIVE_RUN_STATES:
        return RunOverview(status="in_flight", run=await _build_pipeline_run(run, session=session))

    return RunOverview(
        status="terminal",
        outcome=RunOutcome(
            state=run.state,  # type: ignore[arg-type]
            pr_url=ticket.pr_html_url,
            failure_reason=run.failure_reason,
        ),
    )
