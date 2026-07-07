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
terminal event arrives via `core/agent_gateway`'s consumer registry.

A `SkillStage` with `review` configured runs a `main -> (review -> fix)* ->
boundary` loop within ONE `stage_executions` row (`phase` + `iteration`
track progress; no new row per pass — only stage *re-entry*, e.g. a future
send-back, creates a new row). Every reported finding materializes
immediately as a durable `domain/findings` row; the engine applies the
verdict matrix mechanically (`fixed -> resolve`; `still_present` -> `reflag`
when open, `reopen` when resolved; `user_overrode -> dismiss`). The loop
stops (and always proceeds — boundary evaluation is still a labeled
always-proceed stub) once no residuals remain or `review.max_iterations` is
reached. A standalone `ReviewSkillStage` (`kind='review'`) dispatches one
review invocation — no artifact, no loop.

Workspace provision/cleanup are engine-dispatched `kind='system'`
`stage_executions` rows (`stage_index NULL`). Before every skill/review-stage
dispatch `_workspace_is_live` checks liveness via `core/workspace.get_workspace_info`;
a dead or absent workspace triggers `_dispatch_provision_stage` (re-provision),
which overwrites `run.workspace_id` with a freshly minted id. An auth_expired
failure on a skill stage's `main` phase inserts a `refresh-auth` system row,
dispatches `dispatch_auth_refresh`, and on success retries the skill
invocation exactly once (cap tracked in `run.sendback_counts` per stage
name); an auth_expired failure during `review`/`fix` (or on a standalone
review stage) is treated as a plain infra failure — the retry-resume only
knows how to restart at `main`, and restarting the whole stage mid-loop
would lose loop state (see `_handle_skill_stage_event`). An action-only
pipeline never provisions and never runs cleanup.

Every run in `queued` is promoted to `running` by `attempt_promotion`,
guarded by the `ux_pipeline_runs_one_in_flight` partial unique index so a
race between two promotion attempts for the same ticket can never leave two
runs `running` at once — the loser's UPDATE hits the unique violation and
the run stays `queued`.
"""

from __future__ import annotations

from collections.abc import Sequence
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
from app.domain.artifacts import get as get_artifact
from app.domain.artifacts import latest_final, mark_final
from app.domain.artifacts import store as store_artifact
from app.domain.findings import (
    Finding,
    FindingSpec,
    FindingStatusEvent,
    dismiss,
    list_for_stage_execution,
    list_open_for_ticket,
    record_findings,
    reflag,
    reopen,
    resolve,
)
from app.domain.pipelines.contracts import (
    PriorFindingVerdict,
    SkillReturn,
    SkillReviewReturn,
    bucket_confidence,
)
from app.domain.pipelines.definition import ActionStage, FlattenedDefinition, ReviewSkillStage, SkillStage
from app.domain.pipelines.escalation import resolve_escalation_targets
from app.domain.pipelines.models import PipelineRunRow, StageExecutionRow
from app.domain.pipelines.types import Kickoff, PriorFindingRef, RevisionContext, StageInvocationContext
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

    assert isinstance(stage, ReviewSkillStage)
    await _dispatch_review_only_stage(run, stage, stage_index=stage_index, kickoff=kickoff, session=session)


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
    that don't produce one (action stages; `ReviewSkillStage` — no artifact);
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


# ---------------------------------------------------------------------------
# Review loop — findings, verdicts, review/fix dispatch
# ---------------------------------------------------------------------------


async def _resolve_prior_findings(
    run: PipelineRunRow, stage_exec: StageExecutionRow, *, session: AsyncSession
) -> tuple[PriorFindingRef, ...]:
    """Unified rule: this stage execution's own findings (any status) union
    the ticket's open durable findings elsewhere. A comment-response run's
    disputed-regardless-of-status findings arrive with the comment-response
    machinery — not built here."""
    loop_findings = await list_for_stage_execution(stage_exec.id, session=session)
    ticket_open = await list_open_for_ticket(run.org_id, run.ticket_id, session=session)
    by_id: dict[UUID, Finding] = {f.id: f for f in loop_findings}
    for finding in ticket_open:
        by_id.setdefault(finding.id, finding)
    return tuple(
        PriorFindingRef(
            finding_id=finding.id,
            severity=finding.severity,
            body=finding.body,
            code_file=finding.code_file,
            code_line=finding.code_line,
            artifact_section=finding.artifact_section,
        )
        for finding in by_id.values()
    )


def _render_findings_for_fix(findings: Sequence[Finding]) -> str:
    """Markdown-render residual findings as the fix invocation's revision
    text — handle, severity, location, and the finding's own body."""
    lines: list[str] = []
    for finding in findings:
        location = ""
        if finding.code_file and finding.code_line is not None:
            location = f" ({finding.code_file}:{finding.code_line})"
        elif finding.code_file:
            location = f" ({finding.code_file})"
        lines.append(f"- **{finding.handle}** [{finding.severity}]{location}: {finding.body}")
    return "\n".join(lines)


def _last_artifact_id(stage_exec: StageExecutionRow) -> UUID:
    """The most recently produced artifact id for this stage execution
    (main or fix pass) — read back from `loop_state`."""
    for entry in reversed(stage_exec.loop_state):
        artifact_id = entry.get("artifact_id")
        if artifact_id is not None:
            return UUID(artifact_id)
    raise AssertionError(f"stage execution {stage_exec.id} has no recorded artifact")


async def _apply_verdicts(
    run: PipelineRunRow,
    stage_exec: StageExecutionRow,
    verdicts: Sequence[PriorFindingVerdict],
    own_status_by_id: dict[UUID, str],
    *,
    session: AsyncSession,
) -> None:
    """Mechanical verdict matrix, for any review stage: `fixed -> resolve`;
    `still_present -> reflag` when the finding is open, `reopen` when it's
    resolved; `user_overrode -> dismiss`. `status=None` (reply-only, no
    assertion) applies no transition."""
    now = datetime.now(UTC)
    for verdict in verdicts:
        if verdict.status is None:
            continue
        if verdict.status == "fixed":
            event = FindingStatusEvent(
                status="resolved",
                method="review_verdict",
                actor=Actor.system(),
                run_id=run.id,
                stage_execution_id=stage_exec.id,
                at=now,
            )
            await resolve(verdict.finding_id, event=event, session=session)
        elif verdict.status == "still_present":
            current_status = own_status_by_id.get(verdict.finding_id, "open")
            event = FindingStatusEvent(
                status="open",
                method="review_verdict",
                actor=Actor.system(),
                run_id=run.id,
                stage_execution_id=stage_exec.id,
                at=now,
            )
            if current_status == "resolved":
                await reopen(verdict.finding_id, event=event, session=session)
            else:
                await reflag(verdict.finding_id, event=event, session=session)
        elif verdict.status == "user_overrode":
            event = FindingStatusEvent(
                status="dismissed",
                method="user_overrode",
                actor=Actor.system(),
                run_id=run.id,
                stage_execution_id=stage_exec.id,
                at=now,
            )
            await dismiss(verdict.finding_id, event=event, session=session)


async def _record_and_apply_review(
    run: PipelineRunRow,
    stage_exec: StageExecutionRow,
    review_return: SkillReviewReturn,
    *,
    display_prefix: str,
    iteration: int,
    session: AsyncSession,
) -> list[Finding]:
    """Materialize every reported finding, then apply the verdict matrix to
    the findings the skill asserted a status for. Shared by the SkillStage
    review-loop pass and the standalone ReviewSkillStage."""
    specs = [
        FindingSpec(
            id=uuid7(),
            severity=f.severity,
            body=f.body,
            code_file=f.code_file,
            code_line=f.code_line,
            artifact_section=f.artifact_section,
            defect_in_artifact=f.defect_in_artifact,
            display_prefix=display_prefix,
        )
        for f in review_return.new_findings
    ]
    recorded = await record_findings(
        org_id=run.org_id,
        ticket_id=run.ticket_id,
        run_id=run.id,
        stage_name=stage_exec.stage_name,
        stage_execution_id=stage_exec.id,
        iteration=iteration,
        findings=specs,
        session=session,
    )

    own_findings = await list_for_stage_execution(stage_exec.id, session=session)
    own_status_by_id = {f.id: f.status for f in own_findings}
    await _apply_verdicts(
        run, stage_exec, review_return.prior_finding_verdicts, own_status_by_id, session=session
    )
    return recorded


async def _dispatch_review_invocation(
    run: PipelineRunRow,
    stage: SkillStage,
    stage_exec: StageExecutionRow,
    *,
    artifact_body: str,
    session: AsyncSession,
) -> None:
    """Dispatch the review skill over the artifact just produced (main or
    fix pass). Bumps `stage_exec.iteration` — the review-pass counter."""
    assert stage.review is not None
    stage_exec.iteration += 1
    command_id = uuid7()
    ticket = await get_ticket(run.ticket_id, org_id=run.org_id)
    prior_findings = await _resolve_prior_findings(run, stage_exec, session=session)

    invocation_ctx = StageInvocationContext(
        ticket_id=run.ticket_id,
        stage_name=stage.name,
        branch_name=ticket.branch_name or "",
        input=artifact_body,
        prior_findings=prior_findings,
        artifact_path=f"$TMPDIR/{command_id}.md",
    )
    plugin = get_plugin(stage.coding_agent_plugin_id)
    assert run.workspace_id is not None
    invocation = Invocation(
        workspace_id=run.workspace_id,
        skill=stage.review.skill_name,
        model=stage.model,
        effort=stage.effort,
        context={
            **invocation_ctx.model_dump(mode="json"),
            "output_schema": SkillReviewReturn.model_json_schema(),
        },
        wallclock_seconds=stage.wallclock_seconds,
    )
    cmd_ctx = _system_command_context(run, stage_exec)
    await dispatch_invocation(
        invocation=invocation, plugin=plugin, ctx=cmd_ctx, command_id=command_id, session=session
    )
    stage_exec.phase = "review"
    run.pending_agent_command_id = command_id


async def _dispatch_fix_invocation(
    run: PipelineRunRow,
    stage: SkillStage,
    stage_exec: StageExecutionRow,
    *,
    stage_index: int,
    kickoff: Kickoff,
    prior_artifact_body: str,
    residuals: Sequence[Finding],
    session: AsyncSession,
) -> None:
    """Dispatch the main skill fresh with the residual findings rendered as
    `revision(source="fix")` — the same `input` resolution as the original
    main dispatch (upstream artifacts don't change mid-run), plus the fix
    text and the stage's own prior artifact body."""
    command_id = uuid7()
    ticket = await get_ticket(run.ticket_id, org_id=run.org_id)
    input_text = await _resolve_stage_input(run, stage_index, kickoff=kickoff, session=session)
    revision = RevisionContext(
        source="fix", text=_render_findings_for_fix(residuals), prior_artifact=prior_artifact_body
    )

    invocation_ctx = StageInvocationContext(
        ticket_id=run.ticket_id,
        stage_name=stage.name,
        branch_name=ticket.branch_name or "",
        input=input_text,
        revision=revision,
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
    # Durable record of what the fix invocation was asked to revise —
    # independent of the wire payload, so a caller (and tests) can observe
    # "the fix received these findings" straight off the row.
    stage_exec.revision = revision.model_dump(mode="json")
    stage_exec.phase = "fix"
    run.pending_agent_command_id = command_id


async def _dispatch_review_only_stage(
    run: PipelineRunRow, stage: ReviewSkillStage, *, stage_index: int, kickoff: Kickoff, session: AsyncSession
) -> None:
    """`kind='review'`: one invocation speaking `SkillReviewReturn` — no
    artifact, structurally cannot carry a review loop."""
    stage_exec = StageExecutionRow(
        org_id=run.org_id,
        run_id=run.id,
        stage_index=stage_index,
        kind="review",
        stage_name=stage.name,
        skill_name=stage.skill_name,
        status="running",
        phase="review",
        iteration=1,
    )
    session.add(stage_exec)
    await session.flush()

    command_id = uuid7()
    ticket = await get_ticket(run.ticket_id, org_id=run.org_id)
    input_text = await _resolve_stage_input(run, stage_index, kickoff=kickoff, session=session)
    prior_findings = await _resolve_prior_findings(run, stage_exec, session=session)

    invocation_ctx = StageInvocationContext(
        ticket_id=run.ticket_id,
        stage_name=stage.name,
        branch_name=ticket.branch_name or "",
        input=input_text,
        prior_findings=prior_findings,
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
            "output_schema": SkillReviewReturn.model_json_schema(),
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

    if stage_exec.kind == "review":
        await _handle_review_stage_event(
            run, stage_exec, outcome_label=outcome_label, outputs=outputs, session=session
        )
        return

    # Only `kind='skill'` reaches here — `action` never parks (synchronous).
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
        # already used for sendback tracking). Restricted to `phase=='main'`
        # — the resume below always restarts the stage fresh at `main`
        # (`_dispatch_skill_stage`), which would silently discard loop_state
        # and the iteration count if the failure happened mid-loop
        # (`review`/`fix`). A `review`/`fix`-phase auth_expired falls
        # through to the generic infra-failure path below instead.
        if _is_auth_expired(failure_reason) and stage_exec.phase == "main":
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

    flattened = FlattenedDefinition.from_snapshot(run.definition_snapshot)
    stage = flattened.stages[stage_index]
    assert isinstance(stage, SkillStage)
    kickoff = Kickoff.model_validate(run.kickoff)

    if stage_exec.phase == "review":
        await _handle_review_return(
            run, stage, stage_exec, stage_index=stage_index, kickoff=kickoff, outputs=outputs, session=session
        )
        return
    if stage_exec.phase == "fix":
        await _handle_fix_return(run, stage, stage_exec, outputs=outputs, session=session)
        return
    await _handle_main_return(run, stage, stage_exec, outputs=outputs, session=session)


async def _validate_skill_return_and_artifact(
    run: PipelineRunRow,
    stage_exec: StageExecutionRow,
    *,
    stage_index: int,
    outputs: dict[str, Any],
    session: AsyncSession,
) -> tuple[SkillReturn, UUID, str] | None:
    """Validate `outputs` against `SkillReturn` and require an artifact iff
    `outcome == "completed"`. Fails the stage (and the run) and returns
    `None` on any contract violation; otherwise stores + finalizes the
    artifact and returns `(skill_return, artifact_id, artifact_body)`.

    Shared by the `main` and `fix` phases — both dispatch the main skill and
    speak the same `SkillReturn` contract.
    """
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
        return None

    if skill_return.outcome != "completed":
        # `send_back`/`cannot_complete` are run-failure placeholders until
        # the boundary/send-back machinery exists.
        reason = skill_return.outcome_reason or f"stage returned outcome={skill_return.outcome!r}"
        await _fail_stage(run, stage_exec, stage_index=stage_index, failure_reason=reason, session=session)
        return None

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
        return None

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
    # that will never trip. Nothing reads a mid-loop artifact downstream
    # (the run engine only proceeds to the next stage once this one's whole
    # loop finishes), so marking every produced version final is harmless.
    await mark_final(artifact_id, session=session)
    return skill_return, artifact_id, artifact_body


async def _handle_main_return(
    run: PipelineRunRow,
    stage: SkillStage,
    stage_exec: StageExecutionRow,
    *,
    outputs: dict[str, Any],
    session: AsyncSession,
) -> None:
    stage_index = stage_exec.stage_index
    result = await _validate_skill_return_and_artifact(
        run, stage_exec, stage_index=stage_index, outputs=outputs, session=session
    )
    if result is None:
        return
    skill_return, artifact_id, artifact_body = result

    stage_exec.loop_state = [
        *stage_exec.loop_state,
        {"phase": "main", "artifact_id": str(artifact_id), "confidence": skill_return.confidence},
    ]
    stage_exec.confidence = bucket_confidence(skill_return.confidence)
    _publish_artifact_stored(session, run)

    if stage.review is None:
        stage_exec.boundary_outcome = "proceeded"
        stage_exec.status = "completed"
        stage_exec.completed_at = datetime.now(UTC)
        _publish_stage_state(session, run)
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

    await _dispatch_review_invocation(run, stage, stage_exec, artifact_body=artifact_body, session=session)


async def _handle_fix_return(
    run: PipelineRunRow,
    stage: SkillStage,
    stage_exec: StageExecutionRow,
    *,
    outputs: dict[str, Any],
    session: AsyncSession,
) -> None:
    stage_index = stage_exec.stage_index
    result = await _validate_skill_return_and_artifact(
        run, stage_exec, stage_index=stage_index, outputs=outputs, session=session
    )
    if result is None:
        return
    skill_return, artifact_id, artifact_body = result

    stage_exec.loop_state = [
        *stage_exec.loop_state,
        {"phase": "fix", "artifact_id": str(artifact_id), "confidence": skill_return.confidence},
    ]
    stage_exec.confidence = bucket_confidence(skill_return.confidence)
    _publish_artifact_stored(session, run)

    await _dispatch_review_invocation(run, stage, stage_exec, artifact_body=artifact_body, session=session)


async def _handle_review_return(
    run: PipelineRunRow,
    stage: SkillStage,
    stage_exec: StageExecutionRow,
    *,
    stage_index: int,
    kickoff: Kickoff,
    outputs: dict[str, Any],
    session: AsyncSession,
) -> None:
    """One review pass of a SkillStage's attached loop: record findings,
    apply verdicts, then either dispatch a fix (residuals remain and the
    iteration cap isn't hit) or stop the loop and let the stage complete."""
    assert stage.review is not None
    try:
        review_return = SkillReviewReturn.model_validate_json(outputs.get("output", ""))
    except ValidationError as exc:
        await _fail_stage(
            run,
            stage_exec,
            stage_index=stage_index,
            failure_reason=f"SkillReviewReturn schema violation: {exc}",
            session=session,
        )
        return

    recorded = await _record_and_apply_review(
        run,
        stage_exec,
        review_return,
        display_prefix=stage.review.finding_prefix or stage.name,
        iteration=stage_exec.iteration,
        session=session,
    )

    residuals = [
        f for f in await list_for_stage_execution(stage_exec.id, session=session) if f.status == "open"
    ]
    stage_exec.loop_state = [
        *stage_exec.loop_state,
        {
            "phase": "review",
            "iteration": stage_exec.iteration,
            "confidence": review_return.confidence,
            "new_finding_ids": [str(f.id) for f in recorded],
            "residual_finding_ids": [str(f.id) for f in residuals],
        },
    ]
    _publish_stage_state(session, run)

    if residuals and stage_exec.iteration < stage.review.max_iterations:
        artifact = await get_artifact(_last_artifact_id(stage_exec), session=session)
        await _dispatch_fix_invocation(
            run,
            stage,
            stage_exec,
            stage_index=stage_index,
            kickoff=kickoff,
            prior_artifact_body=artifact.body,
            residuals=residuals,
            session=session,
        )
        return

    # Loop stops here — either no residuals or the iteration cap is hit.
    # Boundary evaluation is a labeled stub that always proceeds.
    stage_exec.boundary_outcome = "proceeded"
    stage_exec.status = "completed"
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


async def _handle_review_stage_event(
    run: PipelineRunRow,
    stage_exec: StageExecutionRow,
    *,
    outcome_label: str,
    outputs: dict[str, Any],
    session: AsyncSession,
) -> None:
    """Terminal event for a standalone `kind='review'` stage (`ReviewSkillStage`)
    — one invocation, no artifact, no loop, no auth-retry recovery (see
    `_handle_skill_stage_event` for why the auth-retry resume is narrowed to
    `main`-phase `SkillStage` rows)."""
    stage_index = stage_exec.stage_index

    if outcome_label != "success":
        failure_reason = outputs.get("error_message") or outcome_label
        await _fail_stage(
            run, stage_exec, stage_index=stage_index, failure_reason=failure_reason, session=session
        )
        return

    flattened = FlattenedDefinition.from_snapshot(run.definition_snapshot)
    stage = flattened.stages[stage_index]
    assert isinstance(stage, ReviewSkillStage)

    try:
        review_return = SkillReviewReturn.model_validate_json(outputs.get("output", ""))
    except ValidationError as exc:
        await _fail_stage(
            run,
            stage_exec,
            stage_index=stage_index,
            failure_reason=f"SkillReviewReturn schema violation: {exc}",
            session=session,
        )
        return

    recorded = await _record_and_apply_review(
        run,
        stage_exec,
        review_return,
        display_prefix=stage.finding_prefix or stage.name,
        iteration=1,
        session=session,
    )

    residuals = [
        f for f in await list_for_stage_execution(stage_exec.id, session=session) if f.status == "open"
    ]
    stage_exec.loop_state = [
        *stage_exec.loop_state,
        {
            "phase": "review",
            "iteration": 1,
            "confidence": review_return.confidence,
            "new_finding_ids": [str(f.id) for f in recorded],
            "residual_finding_ids": [str(f.id) for f in residuals],
        },
    ]
    stage_exec.confidence = bucket_confidence(review_return.confidence)
    stage_exec.boundary_outcome = "proceeded"
    stage_exec.status = "completed"
    stage_exec.completed_at = datetime.now(UTC)
    _publish_stage_state(session, run)
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
