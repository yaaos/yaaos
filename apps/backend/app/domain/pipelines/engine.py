"""The run engine — `ROUTE_RUN`/`START_STAGE`/`HANDLE_AGENT_EVENT` taskiq task
bodies driving one `PipelineRun` end to end, mirroring the
`route_workflow`/`start_step`/`handle_agent_event` trio in
`apps/backend/app/core/workflow/service.py:540,113,426`: outbox-atomic
enqueue, SAVEPOINT-wrapped command execution, exception→failure mapping,
`awaiting_agent` parking with a stale-command guard.

`action` stages dispatch synchronously inside `START_STAGE`. `skill` stages
dispatch a real coding-agent invocation via `core/coding_agent.dispatch_invocation`
(engine-minted `command_id`, `StageInvocationContext` as `Invocation.context`)
and park on `pending_agent_command_id`; `HANDLE_AGENT_EVENT` resumes when the
terminal event arrives via `core/agent_gateway`'s consumer registry.  `review`
stages (`kind='review'`) aren't dispatched yet — a run that reaches one fails
loudly with a named reason rather than hanging; the review loop lands with
durable findings.

Workspace provision/cleanup are engine-dispatched `kind='system'`
`stage_executions` rows (`stage_index NULL`). Before every skill/review-stage
dispatch `_workspace_is_live` checks liveness via `core/workspace.get_workspace_info`;
a dead or absent workspace triggers `_dispatch_provision_stage` (re-provision),
which overwrites `run.workspace_id` with a freshly minted id. An auth_expired
failure on a skill stage inserts a `refresh-auth` system row, dispatches
`dispatch_auth_refresh`, and on success retries the skill invocation exactly
once (cap tracked in `run.sendback_counts` per stage name). An action-only
pipeline never provisions and never runs cleanup.

Every run in `queued` is promoted to `running` by `attempt_promotion`,
guarded by the `ux_pipeline_runs_one_in_flight` partial unique index so a
race between two promotion attempts for the same ticket can never leave two
runs `running` at once — the loser's UPDATE hits the unique violation and
the run stays `queued`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid7

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit
from app.core.coding_agent import Invocation, dispatch_invocation, get_plugin
from app.core.database import session as db_session
from app.core.notifications import create as create_notification
from app.core.sse import GeneralEventKind, publish_general_after_commit
from app.core.tasks import TaskRef, enqueue, task
from app.core.workflow import CommandContext
from app.core.workspace import (
    ProvisionWorkspaceSpec,
    WorkspaceNotFoundError,
    WorkspaceStatus,
    dispatch_auth_refresh,
    dispatch_cleanup,
    dispatch_provision,
    get_workspace_info,
)
from app.domain.actions import ActionContext, ActionError, get_action
from app.domain.artifacts import latest_final, mark_final
from app.domain.artifacts import store as store_artifact
from app.domain.pipelines.contracts import SkillReturn, bucket_confidence
from app.domain.pipelines.definition import ActionStage, FlattenedDefinition, SkillStage
from app.domain.pipelines.escalation import resolve_escalation_targets
from app.domain.pipelines.models import PipelineRunRow, StageExecutionRow
from app.domain.pipelines.types import Kickoff, StageInvocationContext
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

# How long a paused run's workspace is kept alive past the pause before the
# normal reaper can collect it. Defined now (the constant is part of the
# stage-lifecycle contract in architecture) but unconsumed until pause
# machinery exists — no run ever pauses in this engine yet.
PAUSED_RUN_WORKSPACE_GRACE_SECONDS = 1800

# Names of the engine-dispatched `kind='system'` bookkeeping stage executions.
_SYSTEM_STAGE_PROVISION = "provision-workspace"
_SYSTEM_STAGE_CLEANUP = "cleanup-workspace"
_SYSTEM_STAGE_REFRESH_AUTH = "refresh-auth"


def _is_auth_expired(failure_reason: str) -> bool:
    """True when the agent reports an expired authentication token."""
    return "auth_expired" in failure_reason.lower()


async def _workspace_is_live(workspace_id: UUID | None) -> bool:
    """Return True iff the workspace row exists and has status='active'.

    Opens its own session (short read — the workspace row is committed state
    by the time any stage dispatch is attempted). Returns False for absent
    rows (`WorkspaceNotFoundError`) and for any non-active status (expired,
    destroying, destroy_failed).
    """
    if workspace_id is None:
        return False
    try:
        info = await get_workspace_info(workspace_id)
        return info.status == WorkspaceStatus.ACTIVE
    except WorkspaceNotFoundError:
        return False


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
        # Residuals/verdicts arrive with the review loop (durable findings
        # don't exist yet); the preceding artifact isn't threaded to actions
        # yet either — all three stay empty until that wiring lands.
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
        await _finish_or_cleanup(run, "failed", failure_reason=failure_reason, session=session)
        return

    # Boundary evaluation is a labeled stub that always proceeds — real
    # conditional/HITL boundary evaluation doesn't exist yet. Cancel is
    # checked at every boundary regardless, including the last one.
    if run.cancel_requested:
        await _finish_or_cleanup(run, "cancelled", failure_reason=None, session=session)
        return

    next_index = completed_stage_index + 1
    if next_index >= total_stages:
        await _finish_or_cleanup(run, "completed", failure_reason=None, session=session)
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


def _system_command_context(run: PipelineRunRow, stage_exec: StageExecutionRow) -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(run.id),
        ticket_id=str(run.ticket_id),
        step_id=str(stage_exec.id),
        attempt=0,
        traceparent=run.otel_trace_context,
    )


async def _finish_or_cleanup(
    run: PipelineRunRow, target_state: str, *, failure_reason: str | None, session: AsyncSession
) -> None:
    """Route a decided run outcome through cleanup first when the run ever
    provisioned a workspace; otherwise enter the terminal state directly.

    `failure_reason` is stashed on the row immediately (even though the run
    isn't terminal yet while cleanup is in flight) so `_finalize_after_cleanup`
    can recompute the same target state once cleanup's terminal event
    arrives, without a second parameter threading through the parked wait.
    """
    run.failure_reason = failure_reason
    if run.workspace_id is not None and run.phase != "cleanup":
        await _dispatch_cleanup_stage(run, session=session)
        return
    await _enter_terminal(run, target_state, failure_reason=failure_reason, session=session)


async def _dispatch_cleanup_stage(run: PipelineRunRow, *, session: AsyncSession) -> None:
    """Dispatch the `cleanup-workspace` system stage. The run's outcome was
    already decided by the caller (stashed via `run.failure_reason` /
    `run.cancel_requested`); `_finalize_after_cleanup` re-derives it once
    cleanup's terminal event arrives."""
    stage_exec = StageExecutionRow(
        org_id=run.org_id,
        run_id=run.id,
        stage_index=None,
        kind="system",
        stage_name=_SYSTEM_STAGE_CLEANUP,
        status="running",
    )
    session.add(stage_exec)
    await session.flush()

    ctx = _system_command_context(run, stage_exec)
    assert run.workspace_id is not None
    command_id = await dispatch_cleanup(run.workspace_id, ctx, session=session)
    run.phase = "cleanup"
    run.pending_agent_command_id = command_id


async def _finalize_after_cleanup(run: PipelineRunRow, *, session: AsyncSession) -> None:
    """Re-derive the terminal state `_finish_or_cleanup` decided before
    dispatching cleanup, and enter it. Cleanup's own outcome (success or
    failure) doesn't change the run's already-decided outcome — a cleanup
    failure is logged by the caller, not re-surfaced here."""
    if run.failure_reason is not None:
        target_state = "failed"
    elif run.cancel_requested:
        target_state = "cancelled"
    else:
        target_state = "completed"
    await _enter_terminal(run, target_state, failure_reason=run.failure_reason, session=session)


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

    # Every skill/review stage needs a live workspace. Provision (or
    # re-provision) when the workspace is absent or dead for any reason —
    # reaper collection, pause-grace expiry, agent restart, absolute TTL.
    # `_dispatch_provision_stage` overwrites `run.workspace_id` with the
    # freshly minted id so a re-provision is structurally identical to the
    # first provision from the engine's perspective.
    # `run.current_stage_index` is already `stage_index` (set by
    # `_dispatch_stage`), so the provision system stage's completion knows
    # which stage to resume after re-provisioning.
    if not await _workspace_is_live(run.workspace_id):
        await _dispatch_provision_stage(run, kickoff=kickoff, session=session)
        return

    if isinstance(stage, SkillStage):
        await _dispatch_skill_stage(run, stage, stage_index=stage_index, kickoff=kickoff, session=session)
        return

    # ReviewSkillStage dispatch needs the review-loop wiring — not built
    # here. Fail the run loudly with a stage_executions row rather than
    # hang forever awaiting an agent command that will never be sent.
    stage_exec = StageExecutionRow(
        org_id=run.org_id,
        run_id=run.id,
        stage_index=stage_index,
        kind=stage.kind,
        stage_name=stage.name,
        skill_name=stage.skill_name,
        status="failed",
        failure_reason="review stage dispatch is not implemented in this engine yet",
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


async def _dispatch_provision_stage(run: PipelineRunRow, *, kickoff: Kickoff, session: AsyncSession) -> None:
    """Dispatch the `provision-workspace` system stage before the stage
    already pinned on `run.current_stage_index`. PR-ticket kickoffs pin a
    detached head-SHA checkout; yaaos-authored tickets check out the
    ticket's named work branch."""
    ticket = await get_ticket(run.ticket_id, org_id=run.org_id)
    stage_exec = StageExecutionRow(
        org_id=run.org_id,
        run_id=run.id,
        stage_index=None,
        kind="system",
        stage_name=_SYSTEM_STAGE_PROVISION,
        status="running",
    )
    session.add(stage_exec)
    await session.flush()

    workspace_id = uuid7()
    if kickoff.pr_head_sha is not None:
        spec = ProvisionWorkspaceSpec(
            workspace_id=workspace_id,
            org_id=run.org_id,
            plugin_id=ticket.plugin_id,
            repo_external_id=ticket.repo_external_id,
            head_sha=kickoff.pr_head_sha,
            base_sha=kickoff.pr_base_sha,
        )
    else:
        spec = ProvisionWorkspaceSpec(
            workspace_id=workspace_id,
            org_id=run.org_id,
            plugin_id=ticket.plugin_id,
            repo_external_id=ticket.repo_external_id,
            branch_name=ticket.branch_name or "",
        )

    ctx = _system_command_context(run, stage_exec)
    command_id = await dispatch_provision(spec, ctx, session=session)
    run.workspace_id = workspace_id
    run.phase = "provision"
    run.pending_agent_command_id = command_id


async def _resolve_stage_input(
    run: PipelineRunRow, stage_index: int, *, kickoff: Kickoff, session: AsyncSession
) -> str:
    """First stage: the kickoff's input text. Else the nearest upstream
    artifact-producing stage's final body, walking back and skipping stages
    that don't produce one (action stages; review stages once they exist);
    falls back to the kickoff input when none is found."""
    if stage_index == 0:
        return kickoff.input_text or ""

    flattened = FlattenedDefinition.from_snapshot(run.definition_snapshot)
    for prior in reversed(flattened.stages[:stage_index]):
        if not isinstance(prior, SkillStage):
            continue
        final = await latest_final(
            org_id=run.org_id, ticket_id=run.ticket_id, stage_name=prior.name, session=session
        )
        if final is not None:
            return final.body
    return kickoff.input_text or ""


async def _dispatch_skill_stage(
    run: PipelineRunRow, stage: SkillStage, *, stage_index: int, kickoff: Kickoff, session: AsyncSession
) -> None:
    """Mint `command_id` first (needed for `artifact_path`), build the
    `StageInvocationContext`, dispatch the invocation, and park on
    `pending_agent_command_id`."""
    stage_exec = StageExecutionRow(
        org_id=run.org_id,
        run_id=run.id,
        stage_index=stage_index,
        kind="skill",
        stage_name=stage.name,
        skill_name=stage.skill_name,
        status="running",
        phase="main",
    )
    session.add(stage_exec)
    await session.flush()

    command_id = uuid7()
    ticket = await get_ticket(run.ticket_id, org_id=run.org_id)
    input_text = await _resolve_stage_input(run, stage_index, kickoff=kickoff, session=session)

    invocation_ctx = StageInvocationContext(
        ticket_id=run.ticket_id,
        stage_name=stage.name,
        branch_name=ticket.branch_name or "",
        input=input_text,
        artifact_path=f"$TMPDIR/{command_id}.md",
    )
    plugin = get_plugin(stage.coding_agent_plugin_id)
    assert run.workspace_id is not None
    invocation = Invocation(
        workspace_id=run.workspace_id,
        skill=stage.skill_name,
        model=stage.model,
        effort=stage.effort,
        context={
            **invocation_ctx.model_dump(mode="json"),
            "output_schema": SkillReturn.model_json_schema(),
        },
        wallclock_seconds=stage.wallclock_seconds,
    )
    cmd_ctx = _system_command_context(run, stage_exec)
    await dispatch_invocation(
        invocation=invocation, plugin=plugin, ctx=cmd_ctx, command_id=command_id, session=session
    )
    run.pending_agent_command_id = command_id


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


# ---------------------------------------------------------------------------
# HANDLE_AGENT_EVENT — resume a run off a terminal AgentEvent
# ---------------------------------------------------------------------------
#
# Same signature as `core/workflow.handle_agent_event` — both engines are
# registered as `core/agent_gateway` consumers and receive the identical
# args dict per terminal event (see `register_agent_event_consumer` in
# `apps/backend/app/core/agent_gateway/service.py`). `workflow_execution_id`
# here is a `pipeline_runs.id` (stringified UUID); an id this engine doesn't
# own (an old-engine `WorkflowExecutionRow` id, or vice-versa) is the normal
# coexistence case, not an error — `session.get` returning `None` is the
# no-op signal.


@task("pipelines.handle_agent_event", queue="pipelines", max_retries=1)
async def handle_agent_event(
    *,
    workflow_execution_id: str,
    agent_command_id: str,
    outcome_label: str,
    outputs: dict[str, Any],
    traceparent: str | None = None,
) -> None:
    del traceparent  # reserved for span-reparenting once spans are added here
    async with db_session() as s:
        await _handle_agent_event_impl(
            run_id=workflow_execution_id,
            agent_command_id=agent_command_id,
            outcome_label=outcome_label,
            outputs=outputs,
            session=s,
        )
        await s.commit()


async def _handle_agent_event_impl(
    *,
    run_id: str,
    agent_command_id: str,
    outcome_label: str,
    outputs: dict[str, Any],
    session: AsyncSession,
) -> None:
    try:
        run = await session.get(PipelineRunRow, UUID(run_id))
    except ValueError:
        # Not even a UUID this engine could have minted — definitely foreign.
        return
    if run is None:
        log.debug("pipelines.handle_agent_event.unknown_run", run_id=run_id)
        return
    if run.state != "running":
        log.debug("pipelines.handle_agent_event.skip_not_running", run_id=run_id, state=run.state)
        return
    if run.pending_agent_command_id is None or str(run.pending_agent_command_id) != agent_command_id:
        log.debug(
            "pipelines.handle_agent_event.stale_command_id",
            run_id=run_id,
            expected=str(run.pending_agent_command_id),
            received=agent_command_id,
        )
        return

    stage_exec = (
        (
            await session.execute(
                select(StageExecutionRow)
                .where(StageExecutionRow.run_id == run.id, StageExecutionRow.status == "running")
                .order_by(StageExecutionRow.started_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if stage_exec is None:
        log.warning("pipelines.handle_agent_event.no_running_stage", run_id=run_id)
        return

    run.pending_agent_command_id = None

    if stage_exec.kind == "system":
        await _handle_system_stage_event(
            run, stage_exec, outcome_label=outcome_label, outputs=outputs, session=session
        )
        return

    # Only `kind='skill'` reaches here today — `review` isn't dispatched yet
    # (see `_start_stage_impl`) and `action` never parks (synchronous).
    await _handle_skill_stage_event(
        run, stage_exec, outcome_label=outcome_label, outputs=outputs, session=session
    )


async def _handle_system_stage_event(
    run: PipelineRunRow,
    stage_exec: StageExecutionRow,
    *,
    outcome_label: str,
    outputs: dict[str, Any],
    session: AsyncSession,
) -> None:
    stage_exec.completed_at = datetime.now(UTC)

    if outcome_label != "success":
        stage_exec.status = "failed"
        stage_exec.failure_reason = outputs.get("error_message") or outcome_label
        if stage_exec.stage_name == _SYSTEM_STAGE_CLEANUP:
            # The run's outcome was already decided before dispatching
            # cleanup; cleanup's own failure doesn't change it.
            log.warning(
                "pipelines.cleanup_stage.failed", run_id=str(run.id), failure_reason=stage_exec.failure_reason
            )
            await _finalize_after_cleanup(run, session=session)
            return
        await _finish_or_cleanup(run, "failed", failure_reason=stage_exec.failure_reason, session=session)
        return

    stage_exec.status = "completed"

    if stage_exec.stage_name == _SYSTEM_STAGE_PROVISION:
        run.phase = "stages"
        await enqueue(
            START_STAGE,
            args={"run_id": str(run.id), "stage_index": run.current_stage_index},
            session=session,
        )
        return

    if stage_exec.stage_name == _SYSTEM_STAGE_CLEANUP:
        await _finalize_after_cleanup(run, session=session)
        return

    if stage_exec.stage_name == _SYSTEM_STAGE_REFRESH_AUTH:
        # Retry the skill stage that triggered this auth-expired recovery.
        flattened = FlattenedDefinition.from_snapshot(run.definition_snapshot)
        retry_stage = flattened.stages[run.current_stage_index]
        kickoff = Kickoff.model_validate(run.kickoff)
        if not isinstance(retry_stage, SkillStage):
            log.error(
                "pipelines.refresh_auth.unexpected_stage_kind",
                run_id=str(run.id),
                stage_kind=retry_stage.kind,
            )
            await _finish_or_cleanup(
                run,
                "failed",
                failure_reason="refresh-auth recovery reached a non-skill stage",
                session=session,
            )
            return
        await _dispatch_skill_stage(
            run, retry_stage, stage_index=run.current_stage_index, kickoff=kickoff, session=session
        )
        return

    log.warning("pipelines.handle_agent_event.unknown_system_stage", stage_name=stage_exec.stage_name)


async def _handle_skill_stage_event(
    run: PipelineRunRow,
    stage_exec: StageExecutionRow,
    *,
    outcome_label: str,
    outputs: dict[str, Any],
    session: AsyncSession,
) -> None:
    stage_index = stage_exec.stage_index

    if outcome_label != "success":
        failure_reason = outputs.get("error_message") or outcome_label
        # Auth-expired recovery: dispatch refresh-auth + retry exactly once.
        # The one-retry cap is tracked in `run.sendback_counts` under a
        # per-stage key so it survives task restarts (the counts map is
        # already used for sendback tracking).
        if _is_auth_expired(failure_reason):
            retry_key = f"auth_retry:{stage_exec.stage_name}"
            counts = dict(run.sendback_counts)
            if counts.get(retry_key, 0) < 1:
                counts[retry_key] = 1
                run.sendback_counts = counts
                stage_exec.status = "failed"
                stage_exec.failure_reason = failure_reason
                stage_exec.completed_at = datetime.now(UTC)
                _publish_stage_state(session, run)
                refresh_exec = StageExecutionRow(
                    org_id=run.org_id,
                    run_id=run.id,
                    stage_index=None,
                    kind="system",
                    stage_name=_SYSTEM_STAGE_REFRESH_AUTH,
                    status="running",
                )
                session.add(refresh_exec)
                await session.flush()
                ctx = _system_command_context(run, refresh_exec)
                assert run.workspace_id is not None
                refresh_command_id = await dispatch_auth_refresh(run.workspace_id, ctx, session=session)
                run.pending_agent_command_id = refresh_command_id
                return
        # Infra failure or auth_expired cap exceeded — fail stage and run.
        await _fail_stage(
            run, stage_exec, stage_index=stage_index, failure_reason=failure_reason, session=session
        )
        return

    try:
        skill_return = SkillReturn.model_validate_json(outputs.get("output", ""))
    except ValidationError as exc:
        await _fail_stage(
            run,
            stage_exec,
            stage_index=stage_index,
            failure_reason=f"SkillReturn schema violation: {exc}",
            session=session,
        )
        return

    if skill_return.outcome != "completed":
        # `send_back`/`cannot_complete` are run-failure placeholders until
        # the boundary/send-back machinery exists.
        reason = skill_return.outcome_reason or f"stage returned outcome={skill_return.outcome!r}"
        await _fail_stage(run, stage_exec, stage_index=stage_index, failure_reason=reason, session=session)
        return

    artifact_body: str | None = None
    artifact_payload = outputs.get("artifact")
    if isinstance(artifact_payload, dict):
        body = artifact_payload.get("body")
        artifact_body = body if isinstance(body, str) else None
    if not artifact_body:
        reason = (
            outputs.get("artifact_error") or "outcome=completed requires an artifact but none was provided"
        )
        await _fail_stage(run, stage_exec, stage_index=stage_index, failure_reason=reason, session=session)
        return

    artifact_id = await store_artifact(
        org_id=run.org_id,
        ticket_id=run.ticket_id,
        run_id=run.id,
        stage_execution_id=stage_exec.id,
        stage_name=stage_exec.stage_name,
        body=artifact_body,
        iteration=stage_exec.iteration,
        session=session,
    )
    # Boundary evaluation is a labeled stub that always proceeds this phase
    # — mark the artifact final immediately rather than waiting on a pause
    # that will never trip. Real boundary evaluation lands with pauses.
    await mark_final(artifact_id, session=session)

    stage_exec.loop_state = [
        *stage_exec.loop_state,
        {"phase": "main", "artifact_id": str(artifact_id), "confidence": skill_return.confidence},
    ]
    stage_exec.confidence = bucket_confidence(skill_return.confidence)
    stage_exec.boundary_outcome = "proceeded"
    stage_exec.status = "completed"
    stage_exec.completed_at = datetime.now(UTC)
    _publish_stage_state(session, run)
    _publish_artifact_stored(session, run)

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


async def _fail_stage(
    run: PipelineRunRow,
    stage_exec: StageExecutionRow,
    *,
    stage_index: int | None,
    failure_reason: str,
    session: AsyncSession,
) -> None:
    stage_exec.status = "failed"
    stage_exec.failure_reason = failure_reason
    stage_exec.completed_at = datetime.now(UTC)
    _publish_stage_state(session, run)
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


def _publish_stage_state(session: AsyncSession, run: PipelineRunRow) -> None:
    publish_general_after_commit(
        session,
        org_id=run.org_id,
        kind=GeneralEventKind.STAGE_STATE_CHANGED,
        payload={"ticket_id": str(run.ticket_id), "run_id": str(run.id)},
    )


def _publish_artifact_stored(session: AsyncSession, run: PipelineRunRow) -> None:
    publish_general_after_commit(
        session,
        org_id=run.org_id,
        kind=GeneralEventKind.ARTIFACT_STORED,
        payload={"ticket_id": str(run.ticket_id)},
    )


# Export the task refs.
ROUTE_RUN: TaskRef = route_run
START_STAGE: TaskRef = start_stage
HANDLE_AGENT_EVENT: TaskRef = handle_agent_event
