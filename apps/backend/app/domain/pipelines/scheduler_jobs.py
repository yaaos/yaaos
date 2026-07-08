"""Periodic `@scheduled` jobs for `domain/pipelines` — schedule-kind trigger
bindings firing on a minute tick, and the stall sweeper that makes Redis
fully disposable for the run engine.

`pipeline_schedule_tick` (`* * * * *`) reads `repos.list_due_schedule_bindings`
each minute and, per due binding, creates a fresh ticket (`source="schedule"`,
`source_external_id="{binding_id}:{fire_time}"` — the tickets unique
constraint makes a redelivered tick a no-op) then starts a run. Cluster
safety for the tick itself is `core/tasks`' own per-slot claim
(`_try_claim` in `core/tasks/scheduler.py`); firing-level idempotency is the
tickets constraint.

`resume_stalled_runs` (`* * * * *`) runs three idempotent reconciliations —
Postgres is the only source of truth for run state, so a Redis flush (or any
lost outbox-dispatched message) converges within one sweep:

- **(a) stale running.** A run sitting `state='running'` with no pending
  agent command and a stale `updated_at` means the next-dispatch message
  (`ROUTE_RUN`, enqueued right after a boundary settled or a run was
  promoted) was durably written to the outbox and dispatched, but the
  Redis message itself never got processed. The recovery args are derived
  from the run's own latest non-system `stage_executions` row — that row IS
  the durable record of "what just settled" (`completed_stage_index` /
  `outcome_label` / `failure_reason`) — so replaying `ROUTE_RUN` reproduces
  exactly what the lost message would have done, whether it was the
  bootstrap dispatch (no such row yet → `completed_stage_index=None`) or a
  settled stage's own next-step routing. System rows (provision/cleanup/
  refresh-auth) are excluded from this lookup because their own completions
  route the next step directly (`START_STAGE` or a synchronous dispatch),
  never through `ROUTE_RUN`'s `completed_stage_index` scheme — for those,
  falling back to the bootstrap replay is exactly correct too, since
  `run.current_stage_index` already pins the stage that should be running.
- **(b) lost resume.** A run's `pending_agent_command_id` points at an
  `agent_commands` row whose `status` is already `"done"` (the terminal
  event WAS recorded — see `core/agent_gateway.record_agent_event` /
  `retire_command`) but the run's own `HANDLE_AGENT_EVENT` resume never
  processed — its outbox message was dispatched then lost (e.g. a Redis
  flush) before a worker consumed it. The original outcome/outputs aren't
  recoverable (`agent_commands` carries no output payload), so the
  synthetic replay uses `outcome_label="failure"` — a conservative,
  loudly-visible failure rather than fabricating a success. Gated on the
  same staleness threshold as (a) so a synthetic resume never races the
  real one for a command that only *just* completed; the stale-command-id
  guard in `engine._handle_agent_event_impl` makes whichever resume
  processes first authoritative regardless.
- **(c) orphaned queued.** A ticket with a `queued` run and no `running`/
  `paused` sibling is a promotion that never happened (or whose own
  promotion attempt was itself lost) — backstop for the promotion race
  `engine.attempt_promotion`/`engine.promote_oldest_queued` already guard at
  the transaction level.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import get_command_status
from app.core.audit_log import Actor, ActorKind
from app.core.auth import org_context
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.tasks import enqueue, scheduled
from app.core.vcs import is_repo_accessible, registered_plugin_ids
from app.domain.pipelines import engine
from app.domain.pipelines import service as pipelines_service
from app.domain.pipelines.models import PipelineRunRow, StageExecutionRow
from app.domain.pipelines.types import Kickoff
from app.domain.repos import DueFire, list_due_schedule_bindings
from app.domain.tickets import create_from_schedule

log = structlog.get_logger("domain.pipelines.scheduler_jobs")


# ---------------------------------------------------------------------------
# pipeline_schedule_tick — schedule-kind trigger bindings fire per minute
# ---------------------------------------------------------------------------


async def _resolve_plugin_id(org_id: UUID, repo_external_id: str) -> str:
    """The VCS plugin that owns `repo_external_id`. Mirrors the
    `domain/repos/web.py` accordion's own resolution (iterate every
    registered plugin) — a schedule binding carries no plugin id of its own
    (its intake point, unlike GitHub's, has no vendor namespace). Skips the
    per-plugin accessibility round trip in the common single-plugin case."""
    plugin_ids = registered_plugin_ids()
    if len(plugin_ids) == 1:
        return plugin_ids[0]
    for plugin_id in plugin_ids:
        if await is_repo_accessible(plugin_id, org_id, repo_external_id):
            return plugin_id
    return plugin_ids[0]


async def _fire_one(fire: DueFire) -> None:
    """Ticket create + `start_run`, one transaction, org-scoped."""
    schedule = fire.binding.schedule
    assert schedule is not None, "list_due_schedule_bindings only returns schedule-kind bindings"
    source_external_id = f"{fire.binding.id}:{fire.fire_time.isoformat()}"

    async with org_context(fire.org_id, ActorKind.SYSTEM):
        async with db_session() as s:
            plugin_id = await _resolve_plugin_id(fire.org_id, fire.binding.repo_external_id)
            ticket_id, created = await create_from_schedule(
                org_id=fire.org_id,
                source_external_id=source_external_id,
                title=schedule.name,
                repo_external_id=fire.binding.repo_external_id,
                plugin_id=plugin_id,
                session=s,
            )
            if created:
                kickoff = Kickoff(
                    intake_point_id="schedule",
                    actor=Actor.system(),
                    input_text=schedule.kickoff_input,
                    notify_user_ids=schedule.notify_user_ids,
                )
                await pipelines_service.start_run(
                    org_id=fire.org_id,
                    ticket_id=ticket_id,
                    pipeline_id=fire.binding.pipeline_id,
                    kickoff=kickoff,
                    session=s,
                )
            await s.commit()


async def pipeline_schedule_tick(*, now: datetime | None = None) -> None:
    """One pass over due schedule bindings. Module-public so service tests
    can invoke the body directly (with a fixed `now`) without going through
    the broker dispatch path — mirrors `core.tasks.tick_once`'s own
    `now: datetime | None = None` convention. A single binding's failure is
    logged and skipped — it must never block the other due bindings in the
    same slot."""
    now = now if now is not None else datetime.now(UTC)
    async with db_session() as s:
        due = await list_due_schedule_bindings(now=now, session=s)
    for fire in due:
        try:
            await _fire_one(fire)
        except Exception:
            log.exception("pipelines.schedule_tick.firing_failed", binding_id=str(fire.binding.id))


# ---------------------------------------------------------------------------
# resume_stalled_runs — three idempotent reconciliations
# ---------------------------------------------------------------------------


async def _reconcile_stale_running(*, cutoff: datetime, session: AsyncSession) -> int:
    candidates = (
        (
            await session.execute(
                select(PipelineRunRow).where(
                    PipelineRunRow.state == "running",
                    PipelineRunRow.pending_agent_command_id.is_(None),
                    PipelineRunRow.updated_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for run in candidates:
        try:
            latest = (
                (
                    await session.execute(
                        select(StageExecutionRow)
                        .where(StageExecutionRow.run_id == run.id, StageExecutionRow.kind != "system")
                        # `id` breaks the tie — rows written in one transaction
                        # share `started_at`, and recovery args derived from the
                        # wrong row would re-execute an already-settled stage.
                        .order_by(StageExecutionRow.started_at.desc(), StageExecutionRow.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if latest is None:
                args = {
                    "run_id": str(run.id),
                    "completed_stage_index": None,
                    "outcome_label": None,
                    "failure_reason": None,
                }
            elif latest.status == "running":
                # Shouldn't coexist with `pending_agent_command_id IS NULL`
                # under normal operation (see module docstring) — surfaced
                # loudly rather than guessed at.
                log.warning(
                    "pipelines.stall_sweep.unexpected_running_stage",
                    run_id=str(run.id),
                    stage_execution_id=str(latest.id),
                )
                continue
            else:
                outcome_label = "success" if latest.status == "completed" else "failure"
                args = {
                    "run_id": str(run.id),
                    "completed_stage_index": latest.stage_index,
                    "outcome_label": outcome_label,
                    "failure_reason": latest.failure_reason if outcome_label == "failure" else None,
                }
            await enqueue(engine.ROUTE_RUN, args=args, session=session)
            log.info("pipelines.stall_sweep.route_run_reenqueued", run_id=str(run.id))
            count += 1
        except Exception:
            log.exception("pipelines.stall_sweep.stale_running_failed", run_id=str(run.id))
    return count


async def _reconcile_lost_resume(*, cutoff: datetime, session: AsyncSession) -> int:
    candidates = (
        (
            await session.execute(
                select(PipelineRunRow).where(
                    PipelineRunRow.state == "running",
                    PipelineRunRow.pending_agent_command_id.is_not(None),
                    PipelineRunRow.updated_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for run in candidates:
        command_id = run.pending_agent_command_id
        assert command_id is not None
        try:
            status = await get_command_status(command_id, session=session)
            if status != "done":
                continue
            await enqueue(
                engine.HANDLE_AGENT_EVENT,
                args={
                    "run_id": str(run.id),
                    "agent_command_id": str(command_id),
                    "outcome_label": "failure",
                    "outputs": {
                        "error_message": (
                            "agent command completed and its terminal event was recorded, but the "
                            "engine resume was never processed (lost after outbox drain) — the "
                            "original outcome could not be recovered"
                        )
                    },
                    "traceparent": None,
                },
                session=session,
            )
            log.info(
                "pipelines.stall_sweep.handle_agent_event_reenqueued",
                run_id=str(run.id),
                agent_command_id=str(command_id),
            )
            count += 1
        except Exception:
            log.exception("pipelines.stall_sweep.lost_resume_failed", run_id=str(run.id))
    return count


async def _reconcile_orphaned_queued(*, session: AsyncSession) -> int:
    ticket_ids = (
        (
            await session.execute(
                select(PipelineRunRow.ticket_id).where(PipelineRunRow.state == "queued").distinct()
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for ticket_id in ticket_ids:
        try:
            in_flight = (
                await session.execute(
                    select(PipelineRunRow.id)
                    .where(
                        PipelineRunRow.ticket_id == ticket_id,
                        PipelineRunRow.state.in_(("running", "paused")),
                    )
                    .limit(1)
                )
            ).first()
            if in_flight is not None:
                continue
            await engine.promote_oldest_queued(ticket_id, session=session)
            log.info("pipelines.stall_sweep.orphaned_queued_promoted", ticket_id=str(ticket_id))
            count += 1
        except Exception:
            log.exception("pipelines.stall_sweep.orphaned_queued_failed", ticket_id=str(ticket_id))
    return count


async def resume_stalled_runs() -> None:
    """One sweep pass. Module-public so service tests can invoke the body
    directly without going through the broker dispatch path."""
    threshold = get_settings().yaaos_run_stall_threshold_seconds
    cutoff = datetime.now(UTC) - timedelta(seconds=threshold)
    async with db_session() as s:
        stale = await _reconcile_stale_running(cutoff=cutoff, session=s)
        lost = await _reconcile_lost_resume(cutoff=cutoff, session=s)
        orphaned = await _reconcile_orphaned_queued(session=s)
        await s.commit()
    if stale or lost or orphaned:
        log.debug(
            "pipelines.stall_sweep.done",
            stale_running=stale,
            lost_resume=lost,
            orphaned_queued=orphaned,
        )


# Per-minute schedule-fire tick — cluster-safe via `core/tasks` per-tick claim.
# Exactly one worker pod enqueues per slot. Body is idempotent.
pipeline_schedule_tick_task = scheduled(
    name="pipeline_schedule_tick", cron="* * * * *", queue="default", max_retries=1
)(pipeline_schedule_tick)

# Per-minute stall sweep — three idempotent reconciliations, see module docstring.
resume_stalled_runs_task = scheduled(
    name="resume_stalled_runs", cron="* * * * *", queue="default", max_retries=1
)(resume_stalled_runs)
