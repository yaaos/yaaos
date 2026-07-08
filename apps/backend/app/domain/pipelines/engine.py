"""The run engine — `ROUTE_RUN`/`START_STAGE`/`HANDLE_AGENT_EVENT` taskiq task
bodies driving one `PipelineRun` end to end: outbox-atomic enqueue,
SAVEPOINT-wrapped command execution, exception→failure mapping,
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
stops once no residuals remain or `review.max_iterations` is reached, then
`boundary.evaluate_boundary` decides pause-vs-proceed (mode + residual
severities + protected paths + bucketed confidence). A standalone
`ReviewSkillStage` (`kind='review'`) dispatches one review invocation — no
artifact, no loop — then the same boundary evaluation.

A tripped boundary (`always_hitl`, or a `conditional` mode with at least one
condition firing) writes a `run_pauses` row, flips the run `paused` and the
ticket `hitl`, extends the workspace's expiry by
`PAUSED_RUN_WORKSPACE_GRACE_SECONDS`, and notifies the resolved escalation
set (`domain/pipelines.escalation` targets, unioned with protected-path
owners when that condition fired). `resolve_pause` (in `service.py`) drives
all four resolutions through the functions below: `approve` (resume at the
next boundary, `resume_from_pause`), `instruct` (fresh stage execution at
the SAME index with the human's text as `revision(source="instruction")`,
`resume_with_instruction`), `send_back` (human-sourced rewind to an earlier
stage, `resume_with_send_back` — same loop-protected machinery as the
automatic send-back below), and `kill` (terminal `killed`, `kill_run`).
`request_cancel` on a `paused` run cancels immediately via `cancel_paused`,
closing the open pause without a resolution.

A main/fix invocation's own `SkillReturn.outcome == "send_back"`, or any
finding still open at boundary time carrying `defect_in_artifact`, triggers
an automatic send-back (`_send_back_to_stage`): validated against an
earlier `SkillStage` in the flattened definition (skill stages are the only
artifact-producing kind a send-back can target); an unresolvable target on
the skill's OWN `send_back_to_stage` is a loud stage failure (a send-back
that can't resolve must not proceed), while an unresolvable
`defect_in_artifact` on a finding degrades to a plain residual (best-effort
attribution, not a contract violation). A target already sent back to once
THIS run pauses instead of rewinding a second time
(`run.sendback_counts[target_stage_name]`, `tripped="sendback_loop"`).

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

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid7

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import DispatchContext
from app.core.audit_log import Actor, audit
from app.core.coding_agent import Invocation, dispatch_invocation, get_plugin
from app.core.database import session as db_session
from app.core.notifications import create as create_notification
from app.core.sse import GeneralEventKind, publish_general_after_commit
from app.core.tasks import TaskRef, enqueue, task
from app.core.workspace import (
    ProvisionWorkspaceSpec,
    WorkspaceNotFoundError,
    WorkspaceStatus,
    dispatch_auth_refresh,
    dispatch_cleanup,
    dispatch_provision,
    extend_expiry,
    get_workspace_info,
)
from app.domain.actions import ActionContext, ActionError, StageVerdict, get_action
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
from app.domain.findings import get as get_finding
from app.domain.pipelines.boundary import BoundaryDecision, evaluate_boundary
from app.domain.pipelines.contracts import (
    PriorFindingVerdict,
    SkillReturn,
    SkillReviewReturn,
    bucket_confidence,
    merge_prior_findings,
)
from app.domain.pipelines.definition import ActionStage, FlattenedDefinition, ReviewSkillStage, SkillStage
from app.domain.pipelines.escalation import resolve_escalation_targets
from app.domain.pipelines.models import PipelineRunRow, RunPauseRow, StageExecutionRow
from app.domain.pipelines.types import (
    Kickoff,
    PRContext,
    PriorFindingRef,
    RevisionContext,
    StageInvocationContext,
)
from app.domain.tickets import (
    Ticket,
    get_pull_request,
    set_current_run,
    transition_ticket_on_run_paused,
    transition_ticket_on_run_resumed,
    transition_ticket_on_run_start,
    transition_ticket_on_run_terminal,
)
from app.domain.tickets import (
    get as get_ticket,
)

log = structlog.get_logger("domain.pipelines.engine")

_RUN_STATE_TO_TICKET_STATUS = {
    "completed": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "killed": "cancelled",
}

# How long a paused run's workspace is kept alive past the pause before the
# normal reaper can collect it.
PAUSED_RUN_WORKSPACE_GRACE_SECONDS = 1800

# Names of the engine-dispatched `kind='system'` bookkeeping stage executions.
_SYSTEM_STAGE_PROVISION = "provision-workspace"
_SYSTEM_STAGE_CLEANUP = "cleanup-workspace"
_SYSTEM_STAGE_REFRESH_AUTH = "refresh-auth"

# Run-terminal hook registry — list-shaped like `core/agent_gateway`'s
# consumer registry. Each hook gets its own outbox enqueue, inside the same
# transaction that flips the run terminal (`_enter_terminal`). Consumed
# today by `domain/pr_review` (comment batching).
_run_terminal_hooks: list[TaskRef] = []


def register_run_terminal_hook(task_ref: TaskRef) -> None:
    """Append `task_ref` to the run-terminal hook registry. Called at
    import time by any module that wants to react to every pipeline run
    reaching a terminal state."""
    _run_terminal_hooks.append(task_ref)


# Comment-findings provider — registered once, at `domain/pr_review` import
# time (mirrors `domain/repos.register_pipeline_lookup`'s cycle-avoidance:
# `pr_review` already depends on `pipelines`, so the reverse edge would
# cycle). Supplies the finding ids referenced by a comment-response run's
# claimed batch — see `_resolve_prior_findings`.
_CommentFindingIdsProvider = Callable[[UUID, AsyncSession], Awaitable[tuple[UUID, ...]]]
_comment_finding_ids_provider: _CommentFindingIdsProvider | None = None


def register_comment_findings_provider(fn: _CommentFindingIdsProvider) -> None:
    """Registered once, at `domain/pr_review` import time. Re-registering
    overwrites (mirrors `core/byok.register_validator`'s reload tolerance)."""
    global _comment_finding_ids_provider
    _comment_finding_ids_provider = fn


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
# Pause creation + resolution
# ---------------------------------------------------------------------------


class _RunPausedPayload(BaseModel):
    ticket_id: str
    pipeline_name: str
    stage_name: str
    tripped: dict[str, Any]


async def _enter_pause(
    run: PipelineRunRow,
    stage_exec: StageExecutionRow,
    decision: BoundaryDecision,
    *,
    kickoff: Kickoff,
    session: AsyncSession,
) -> None:
    """A boundary tripped: record the pause, flip run + ticket state, extend
    the workspace's grace window, and notify the escalation set (resolved
    targets unioned with protected-path owners when that condition fired).
    """
    stage_exec.boundary_outcome = "paused"
    stage_exec.boundary_detail = decision.tripped

    escalation_targets = await resolve_escalation_targets(kickoff, run.org_id, session=session)
    escalation_targets |= set(decision.protected_owner_user_ids)

    pause = RunPauseRow(
        org_id=run.org_id,
        run_id=run.id,
        stage_execution_id=stage_exec.id,
        tripped=decision.tripped,
        escalation_user_ids=list(escalation_targets),
    )
    session.add(pause)
    await session.flush()

    run.state = "paused"
    if run.workspace_id is not None:
        await extend_expiry(run.workspace_id, seconds=PAUSED_RUN_WORKSPACE_GRACE_SECONDS, session=session)
    await transition_ticket_on_run_paused(run.ticket_id, org_id=run.org_id, run_id=run.id, session=session)
    _publish_run_state(session, run)

    await audit(
        "pipeline_run",
        run.id,
        "run.paused",
        _RunPausedPayload(
            ticket_id=str(run.ticket_id),
            pipeline_name=run.pipeline_name,
            stage_name=stage_exec.stage_name,
            tripped=decision.tripped,
        ),
        actor=Actor.system(),
        org_id=run.org_id,
        session=session,
    )

    for user_id in escalation_targets:
        await create_notification(
            user_id=user_id,
            org_id=run.org_id,
            type="pipeline_run_paused",
            title=f"{run.pipeline_name} needs your input",
            body=f"{stage_exec.stage_name} is waiting on a decision.",
            subject_type="run_pause",
            subject_id=pause.id,
            session=session,
        )


async def get_open_pause_for_run(run_id: UUID, *, session: AsyncSession) -> RunPauseRow | None:
    """The pause row currently blocking `run_id`, if any (`resolved_at IS
    NULL`). A `paused` run always has exactly one — used by
    `service.request_cancel` to close it on an immediate cancel."""
    return (
        await session.execute(
            select(RunPauseRow).where(RunPauseRow.run_id == run_id, RunPauseRow.resolved_at.is_(None))
        )
    ).scalar_one_or_none()


async def resume_from_pause(pause: RunPauseRow, run: PipelineRunRow, *, session: AsyncSession) -> None:
    """`approve`: resume at the next boundary. The paused stage already
    completed its own work (`stage_executions.status='completed'`), so this
    replays the exact `ROUTE_RUN(completed_stage_index=..., outcome_label=
    "success")` call the stage would have made itself had the boundary not
    tripped."""
    stage_exec = await session.get(StageExecutionRow, pause.stage_execution_id)
    assert stage_exec is not None
    run.state = "running"
    await transition_ticket_on_run_resumed(run.ticket_id, org_id=run.org_id, run_id=run.id, session=session)
    _publish_run_state(session, run)
    await enqueue(
        ROUTE_RUN,
        args={
            "run_id": str(run.id),
            "completed_stage_index": stage_exec.stage_index,
            "outcome_label": "success",
            "failure_reason": None,
        },
        session=session,
    )


async def resume_with_instruction(
    pause: RunPauseRow, run: PipelineRunRow, *, instruction: str, session: AsyncSession
) -> None:
    """`instruct`: a fresh `stage_executions` row at the SAME `stage_index`
    as the paused one, with the human's text as `revision(source=
    "instruction")` and the stage's own latest final artifact (if any) as
    `prior_artifact` — same shape as a fix pass, human-sourced. Same run
    continues (not a new run)."""
    stage_exec = await session.get(StageExecutionRow, pause.stage_execution_id)
    assert stage_exec is not None
    assert stage_exec.stage_index is not None
    flattened = FlattenedDefinition.from_snapshot(run.definition_snapshot)
    stage = flattened.stages[stage_exec.stage_index]
    assert isinstance(stage, SkillStage | ReviewSkillStage)
    kickoff = Kickoff.model_validate(run.kickoff)

    prior = await latest_final(
        org_id=run.org_id, ticket_id=run.ticket_id, stage_name=stage.name, session=session
    )
    revision = RevisionContext(
        source="instruction", text=instruction, prior_artifact=prior.body if prior is not None else ""
    )
    run.kickoff = kickoff.model_copy(update={"revision": revision}).model_dump(mode="json")
    run.current_stage_index = stage_exec.stage_index
    run.state = "running"
    await transition_ticket_on_run_resumed(run.ticket_id, org_id=run.org_id, run_id=run.id, session=session)
    _publish_run_state(session, run)
    await _start_stage_impl(run_id=run.id, stage_index=stage_exec.stage_index, session=session)


async def resume_with_send_back(
    pause: RunPauseRow,
    run: PipelineRunRow,
    *,
    stage_exec: StageExecutionRow,
    target_index: int,
    target_stage: SkillStage,
    session: AsyncSession,
) -> None:
    """`send_back`: human-sourced rewind — same loop-protected machinery as
    the automatic path (`_send_back_to_stage`). Caller (`service.py`) has
    already resolved + validated `target_index`/`target_stage` against the
    paused stage's own flattened definition."""
    kickoff = Kickoff.model_validate(run.kickoff)
    run.state = "running"
    await transition_ticket_on_run_resumed(run.ticket_id, org_id=run.org_id, run_id=run.id, session=session)
    _publish_run_state(session, run)
    await _send_back_to_stage(
        run,
        stage_exec,
        target_index=target_index,
        target_stage=target_stage,
        gap_text="",
        kickoff=kickoff,
        session=session,
    )


async def kill_run(run: PipelineRunRow, *, session: AsyncSession) -> None:
    """`kill`: run terminal `killed` — same cleanup-first routing as any
    other decided run outcome. Flips `state` back to `running` first (the
    "a dispatch is in flight" state, regardless of `paused`'s HITL meaning)
    so the cleanup stage's own terminal event — routed through
    `HANDLE_AGENT_EVENT`, which requires `state == "running"` — isn't
    dropped as stale."""
    run.state = "running"
    await _finish_or_cleanup(run, "killed", failure_reason=None, session=session)


async def cancel_paused(run: PipelineRunRow, pause: RunPauseRow, *, session: AsyncSession) -> None:
    """`request_cancel` on a `paused` run: cancels immediately. Closes the
    open pause (`resolved_at` stamped, no `resolution` recorded — the run is
    being cancelled out from under it, not resolved), flips `state` back to
    `running` for the same reason `kill_run` does, sets `cancel_requested`
    (the same signal `_finalize_after_cleanup` already reads for a
    running-run cancel — no separate detection needed), and routes through
    the normal terminal path (cleanup-workspace first — a paused run always
    already provisioned one)."""
    pause.resolved_at = datetime.now(UTC)
    run.state = "running"
    run.cancel_requested = True
    await _finish_or_cleanup(run, "cancelled", failure_reason=None, session=session)


# ---------------------------------------------------------------------------
# PRContext assembly — how review skills know what to diff, and how actions
# know which PR to post to.
# ---------------------------------------------------------------------------


async def _prev_reviewed_head_sha(
    ticket_id: UUID, *, exclude_run_id: UUID, session: AsyncSession
) -> str | None:
    """Head SHA of the ticket's last COMPLETED run that carried a PR kickoff
    (`Kickoff.pr_head_sha` set) — the incremental-review diff floor. `None`
    on the ticket's first PR review. No dedicated column: derived by walking
    completed runs newest-first and reading each one's own `kickoff` JSONB."""
    rows = (
        (
            await session.execute(
                select(PipelineRunRow)
                .where(
                    PipelineRunRow.ticket_id == ticket_id,
                    PipelineRunRow.state == "completed",
                    PipelineRunRow.id != exclude_run_id,
                )
                .order_by(PipelineRunRow.completed_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        prior_kickoff = Kickoff.model_validate(row.kickoff)
        if prior_kickoff.pr_head_sha is not None:
            return prior_kickoff.pr_head_sha
    return None


async def _build_pr_context(
    run: PipelineRunRow, kickoff: Kickoff, ticket: Ticket, *, session: AsyncSession
) -> PRContext | None:
    """`None` when the ticket has no PR, or this run's own kickoff didn't pin
    a head SHA (a non-PR-triggered run on a PR ticket, e.g. a schedule
    kickoff) — head/base always come from THIS run's kickoff pins, never the
    PR row's own (possibly stale) fields."""
    if ticket.pr_id is None or kickoff.pr_head_sha is None:
        return None
    pr = await get_pull_request(ticket.pr_id, org_id=run.org_id)
    prev_reviewed_head_sha = await _prev_reviewed_head_sha(
        run.ticket_id, exclude_run_id=run.id, session=session
    )
    return PRContext(
        pr_external_id=pr.external_id,
        head_sha=kickoff.pr_head_sha,
        base_sha=kickoff.pr_base_sha or "",
        prev_reviewed_head_sha=prev_reviewed_head_sha,
    )


# ---------------------------------------------------------------------------
# ActionContext assembly
# ---------------------------------------------------------------------------


async def _latest_stage_execution(
    run_id: UUID, stage_index: int, *, session: AsyncSession
) -> StageExecutionRow | None:
    """The most recent stage-execution row at `stage_index` on this run —
    the one that just settled leading into the current dispatch (re-entries
    create new rows at the same index, so "latest" is the live one)."""
    return (
        (
            await session.execute(
                select(StageExecutionRow)
                .where(StageExecutionRow.run_id == run_id, StageExecutionRow.stage_index == stage_index)
                .order_by(StageExecutionRow.started_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


def _last_review_verdicts(stage_exec: StageExecutionRow) -> list[dict[str, Any]]:
    """The last `review`-phase `loop_state` entry's recorded verdicts (see
    `_handle_review_return`/`_handle_review_stage_event`) — mirrors
    `_last_artifact_id`/`_last_paths_affected`'s reversed-scan idiom. Empty
    when the stage never ran a review pass (a plain `SkillStage` with no
    `review` configured, or an action-kind preceding stage)."""
    for entry in reversed(stage_exec.loop_state):
        if entry.get("phase") == "review":
            return entry.get("verdicts", [])
    return []


async def _build_action_context(
    run: PipelineRunRow, kickoff: Kickoff, *, stage_index: int, org_id: UUID, session: AsyncSession
) -> ActionContext:
    ticket = await get_ticket(run.ticket_id, org_id=org_id)
    pr_external_id: str | None = None
    if ticket.pr_id is not None:
        pr = await get_pull_request(ticket.pr_id, org_id=org_id)
        pr_external_id = pr.external_id

    preceding_residuals: tuple[Finding, ...] = ()
    preceding_verdicts: tuple[StageVerdict, ...] = ()
    preceding_artifact_id: UUID | None = None
    if stage_index > 0:
        preceding = await _latest_stage_execution(run.id, stage_index - 1, session=session)
        if preceding is not None:
            own_findings = await list_for_stage_execution(preceding.id, session=session)
            preceding_residuals = tuple(f for f in own_findings if f.status == "open")
            preceding_verdicts = tuple(
                StageVerdict(finding_id=UUID(v["finding_id"]), status=v.get("status"), reply=v.get("reply"))
                for v in _last_review_verdicts(preceding)
            )
            if preceding.kind == "skill":
                preceding_artifact_id = _last_artifact_id(preceding)

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
        preceding_residuals=preceding_residuals,
        preceding_verdicts=preceding_verdicts,
        preceding_artifact_id=preceding_artifact_id,
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
        # `run.current_stage_index` is `None` for a normal `start_run` (index
        # 0), but `start_rerun_from_stage` pins it to `from_stage`'s index at
        # row creation so a rerun's bootstrap jumps straight there.
        start_index = run.current_stage_index if run.current_stage_index is not None else 0
        await _dispatch_stage(run, stage_index=start_index, session=session)
        return

    if outcome_label == "failure":
        await _finish_or_cleanup(run, "failed", failure_reason=failure_reason, session=session)
        return

    # By the time ROUTE_RUN sees `outcome_label == "success"`, the completed
    # stage's own `BoundaryControl` (see `boundary.evaluate_boundary`) has
    # already decided to proceed — a `pause` decision routes through
    # `_enter_pause` instead and never reaches here. ROUTE_RUN's own job is
    # next-stage-or-terminal routing plus the cancel check, which fires at
    # every boundary regardless, including the last one.
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

    for hook in _run_terminal_hooks:
        await enqueue(
            hook, args={"org_id": str(run.org_id), "ticket_id": str(run.ticket_id)}, session=session
        )

    await promote_oldest_queued(run.ticket_id, session=session)


def _system_command_context(run: PipelineRunRow, stage_exec: StageExecutionRow) -> DispatchContext:
    return DispatchContext(
        run_id=run.id,
        ticket_id=run.ticket_id,
        stage_execution_id=stage_exec.id,
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


async def _run_was_killed(run_id: UUID, *, session: AsyncSession) -> bool:
    """True iff a `run_pauses` row on this run resolved `kill` — the signal
    `_finalize_after_cleanup` uses to distinguish a killed run from a
    normal completion, without a dedicated column: `kill` is terminal, so a
    run can carry at most one such row."""
    return (
        await session.execute(
            select(RunPauseRow.id).where(RunPauseRow.run_id == run_id, RunPauseRow.resolution == "kill")
        )
    ).first() is not None


async def _finalize_after_cleanup(run: PipelineRunRow, *, session: AsyncSession) -> None:
    """Re-derive the terminal state `_finish_or_cleanup` decided before
    dispatching cleanup, and enter it. Cleanup's own outcome (success or
    failure) doesn't change the run's already-decided outcome — a cleanup
    failure is logged by the caller, not re-surfaced here."""
    if run.failure_reason is not None:
        target_state = "failed"
    elif run.cancel_requested:
        target_state = "cancelled"
    elif await _run_was_killed(run.id, session=session):
        target_state = "killed"
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

    # Single-use revision carried on `kickoff` by a re-entry that needed a
    # provision hop first (`start_rerun_from_stage`'s brand-new run, or a
    # mid-stage re-provision triggered by `resume_with_instruction`/
    # `_send_back_to_stage`): thread it onto THIS dispatch, then clear it so
    # a later stage or a later re-provision of a DIFFERENT stage never
    # replays it.
    revision = kickoff.revision
    if revision is not None:
        run.kickoff = kickoff.model_copy(update={"revision": None}).model_dump(mode="json")

    if isinstance(stage, SkillStage):
        await _dispatch_skill_stage(
            run, stage, stage_index=stage_index, kickoff=kickoff, session=session, revision=revision
        )
        return

    assert isinstance(stage, ReviewSkillStage)
    await _dispatch_review_only_stage(
        run, stage, stage_index=stage_index, kickoff=kickoff, session=session, revision=revision
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
    run: PipelineRunRow,
    stage: SkillStage,
    *,
    stage_index: int,
    kickoff: Kickoff,
    session: AsyncSession,
    revision: RevisionContext | None = None,
) -> None:
    """Mint `command_id` first (needed for `artifact_path`), build the
    `StageInvocationContext`, dispatch the invocation, and park on
    `pending_agent_command_id`. `revision` rides re-entries (instruct,
    send-back, rerun-from-stage) onto the fresh invocation; `None` for a
    normal forward dispatch."""
    stage_exec = StageExecutionRow(
        org_id=run.org_id,
        run_id=run.id,
        stage_index=stage_index,
        kind="skill",
        stage_name=stage.name,
        skill_name=stage.skill_name,
        status="running",
        phase="main",
        # Durable record of a re-entry's revision, independent of the wire
        # payload — same idiom as `_dispatch_fix_invocation`'s own
        # `stage_exec.revision` stamp.
        revision=revision.model_dump(mode="json") if revision is not None else None,
    )
    session.add(stage_exec)
    await session.flush()

    command_id = uuid7()
    ticket = await get_ticket(run.ticket_id, org_id=run.org_id)
    input_text = await _resolve_stage_input(run, stage_index, kickoff=kickoff, session=session)
    pr = await _build_pr_context(run, kickoff, ticket, session=session)

    invocation_ctx = StageInvocationContext(
        ticket_id=run.ticket_id,
        stage_name=stage.name,
        branch_name=ticket.branch_name or "",
        input=input_text,
        pr=pr,
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
    run.pending_agent_command_id = command_id


# ---------------------------------------------------------------------------
# Review loop — findings, verdicts, review/fix dispatch
# ---------------------------------------------------------------------------


async def _resolve_prior_findings(
    run: PipelineRunRow, stage_exec: StageExecutionRow, *, session: AsyncSession
) -> tuple[PriorFindingRef, ...]:
    """Unified rule (`contracts.merge_prior_findings`): this stage
    execution's own findings (any status) union the ticket's open durable
    findings elsewhere union — for a comment-response run — findings
    referenced by the batch's comments regardless of status, via the
    registered `domain/pr_review` provider (empty/no-op for any other run)."""
    loop_findings = await list_for_stage_execution(stage_exec.id, session=session)
    ticket_open = await list_open_for_ticket(run.org_id, run.ticket_id, session=session)
    comment_findings: list[Finding] = []
    if _comment_finding_ids_provider is not None:
        for finding_id in await _comment_finding_ids_provider(run.id, session):
            comment_findings.append(await get_finding(finding_id, session=session))
    return merge_prior_findings(loop_findings, ticket_open, comment_findings)


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


def _last_paths_affected(stage_exec: StageExecutionRow) -> list[str]:
    """The most recently reported `SkillReturn.paths_affected` for this
    stage execution (main or fix pass) — read back from `loop_state`,
    mirroring `_last_artifact_id`. Used by the boundary evaluation once a
    review loop stops, since only the main/fix passes report paths (review
    passes speak `SkillReviewReturn`, which carries no such field)."""
    for entry in reversed(stage_exec.loop_state):
        paths = entry.get("paths_affected")
        if paths is not None:
            return paths
    return []


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
    kickoff = Kickoff.model_validate(run.kickoff)
    prior_findings = await _resolve_prior_findings(run, stage_exec, session=session)
    pr = await _build_pr_context(run, kickoff, ticket, session=session)

    invocation_ctx = StageInvocationContext(
        ticket_id=run.ticket_id,
        stage_name=stage.name,
        branch_name=ticket.branch_name or "",
        input=artifact_body,
        pr=pr,
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
    pr = await _build_pr_context(run, kickoff, ticket, session=session)
    revision = RevisionContext(
        source="fix", text=_render_findings_for_fix(residuals), prior_artifact=prior_artifact_body
    )

    invocation_ctx = StageInvocationContext(
        ticket_id=run.ticket_id,
        stage_name=stage.name,
        branch_name=ticket.branch_name or "",
        input=input_text,
        pr=pr,
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
    run: PipelineRunRow,
    stage: ReviewSkillStage,
    *,
    stage_index: int,
    kickoff: Kickoff,
    session: AsyncSession,
    revision: RevisionContext | None = None,
) -> None:
    """`kind='review'`: one invocation speaking `SkillReviewReturn` — no
    artifact, structurally cannot carry a review loop. `revision` rides an
    `instruct` re-entry onto a standalone review stage's re-dispatch."""
    stage_exec = StageExecutionRow(
        org_id=run.org_id,
        run_id=run.id,
        stage_index=stage_index,
        kind="review",
        stage_name=stage.name,
        skill_name=stage.skill_name,
        status="running",
        phase="review",
        revision=revision.model_dump(mode="json") if revision is not None else None,
        iteration=1,
    )
    session.add(stage_exec)
    await session.flush()

    command_id = uuid7()
    ticket = await get_ticket(run.ticket_id, org_id=run.org_id)
    input_text = await _resolve_stage_input(run, stage_index, kickoff=kickoff, session=session)
    prior_findings = await _resolve_prior_findings(run, stage_exec, session=session)
    pr = await _build_pr_context(run, kickoff, ticket, session=session)

    invocation_ctx = StageInvocationContext(
        ticket_id=run.ticket_id,
        stage_name=stage.name,
        branch_name=ticket.branch_name or "",
        input=input_text,
        pr=pr,
        revision=revision,
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

    ctx = await _build_action_context(
        run, kickoff, stage_index=stage_index, org_id=run.org_id, session=session
    )

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
    # writing the failure onto it.
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
# Registered as a `core/agent_gateway` consumer (see
# `register_agent_event_consumer` in `apps/backend/app/core/agent_gateway/service.py`)
# and receives the args dict for every terminal event. `run_id`
# here is a `pipeline_runs.id` (stringified UUID); an id this engine doesn't
# own (e.g. a stale/foreign id) is a no-op, not an error — `session.get`
# returning `None` is the signal.


@task("pipelines.handle_agent_event", queue="pipelines", max_retries=1)
async def handle_agent_event(
    *,
    run_id: str,
    agent_command_id: str,
    outcome_label: str,
    outputs: dict[str, Any],
    traceparent: str | None = None,
) -> None:
    del traceparent  # reserved for span-reparenting once spans are added here
    async with db_session() as s:
        await _handle_agent_event_impl(
            run_id=run_id,
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
        await _handle_fix_return(run, stage, stage_exec, kickoff=kickoff, outputs=outputs, session=session)
        return
    await _handle_main_return(run, stage, stage_exec, kickoff=kickoff, outputs=outputs, session=session)


def resolve_send_back_target(
    flattened: FlattenedDefinition,
    *,
    stage: SkillStage | ReviewSkillStage,
    before_index: int,
    target_name: str,
) -> tuple[int, SkillStage] | None:
    """The upstream `SkillStage` named `target_name`, if valid: an earlier
    stage (index < `before_index`) in the flattened definition — skill
    stages are the only artifact-producing kind a send-back can target —
    respecting `stage.context_stages` when it restricts which upstream
    stages are shown (`None` = all upstream stages visible). Intra-module
    helper — `service.py` reuses it to validate a human-sourced `send_back`
    resolution before calling `resume_with_send_back`."""
    context_stages = stage.context_stages
    for idx, candidate in enumerate(flattened.stages[:before_index]):
        if isinstance(candidate, SkillStage) and candidate.name == target_name:
            if context_stages is not None and target_name not in context_stages:
                return None
            return idx, candidate
    return None


def _resolve_residual_send_back(
    flattened: FlattenedDefinition,
    *,
    stage: SkillStage | ReviewSkillStage,
    residuals: Sequence[Finding],
    before_index: int,
    run_id: UUID,
) -> tuple[int, SkillStage, str] | None:
    """The first open residual carrying a `defect_in_artifact` that resolves
    to a valid upstream `SkillStage`, if any. An unresolvable name degrades
    to a plain residual — logged, never a contract violation (best-effort
    attribution by the skill, unlike a main skill's own self-reported
    `send_back_to_stage`)."""
    for finding in residuals:
        if not finding.defect_in_artifact:
            continue
        resolved = resolve_send_back_target(
            flattened, stage=stage, before_index=before_index, target_name=finding.defect_in_artifact
        )
        if resolved is None:
            log.info(
                "pipelines.residual_defect_in_artifact.unresolvable",
                run_id=str(run_id),
                target=finding.defect_in_artifact,
            )
            continue
        target_index, target_stage = resolved
        return target_index, target_stage, finding.body
    return None


async def _send_back_to_stage(
    run: PipelineRunRow,
    stage_exec: StageExecutionRow,
    *,
    target_index: int,
    target_stage: SkillStage,
    gap_text: str,
    kickoff: Kickoff,
    session: AsyncSession,
) -> None:
    """Loop-protected rewind to `target_stage`. Caller has already flipped
    `stage_exec` to its terminal `status`/`completed_at` — this only decides
    pause-vs-rewind and (on rewind) dispatches the target fresh via
    `_start_stage_impl` (workspace-liveness-checked, re-provisioning if
    needed — same safety net every forward dispatch gets).

    `run.sendback_counts` is keyed by the TARGET stage name (bare — distinct
    from the `auth_retry:{stage}` keys the auth-expired retry uses under its
    own namespace, and stage names can never contain a colon).
    """
    counts = dict(run.sendback_counts)
    if counts.get(target_stage.name, 0) >= 1:
        decision = BoundaryDecision(outcome="pause", tripped={"sendback_loop": target_stage.name})
        await _enter_pause(run, stage_exec, decision, kickoff=kickoff, session=session)
        return

    counts[target_stage.name] = 1
    run.sendback_counts = counts
    stage_exec.boundary_outcome = "sent_back"
    _publish_stage_state(session, run)

    prior = await latest_final(
        org_id=run.org_id, ticket_id=run.ticket_id, stage_name=target_stage.name, session=session
    )
    revision = RevisionContext(
        source="send_back", text=gap_text, prior_artifact=prior.body if prior is not None else ""
    )
    run.kickoff = kickoff.model_copy(update={"revision": revision}).model_dump(mode="json")
    run.current_stage_index = target_index
    await _start_stage_impl(run_id=run.id, stage_index=target_index, session=session)


async def _validate_skill_return_and_artifact(
    run: PipelineRunRow,
    stage: SkillStage,
    stage_exec: StageExecutionRow,
    *,
    stage_index: int,
    kickoff: Kickoff,
    outputs: dict[str, Any],
    session: AsyncSession,
) -> tuple[SkillReturn, UUID, str] | None:
    """Validate `outputs` against `SkillReturn`. `cannot_complete` fails the
    stage (and the run). `send_back` — validated against an earlier
    `SkillStage` in the flattened definition, else a loud stage failure —
    routes through `_send_back_to_stage` instead of producing an artifact.
    `completed` requires an artifact; fails the stage if missing. Returns
    `None` for every outcome except a validated `completed` (which stores +
    finalizes the artifact and returns `(skill_return, artifact_id,
    artifact_body)`).

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

    if skill_return.outcome == "cannot_complete":
        reason = skill_return.outcome_reason or "stage reported cannot_complete"
        await _fail_stage(run, stage_exec, stage_index=stage_index, failure_reason=reason, session=session)
        return None

    if skill_return.outcome == "send_back":
        target_name = skill_return.send_back_to_stage
        flattened = FlattenedDefinition.from_snapshot(run.definition_snapshot)
        resolved = (
            resolve_send_back_target(
                flattened, stage=stage, before_index=stage_index, target_name=target_name
            )
            if target_name
            else None
        )
        if resolved is None:
            await _fail_stage(
                run,
                stage_exec,
                stage_index=stage_index,
                failure_reason=f"send_back_to_stage {target_name!r} is not a valid upstream stage",
                session=session,
            )
            return None
        target_index, target_stage = resolved
        stage_exec.status = "completed"
        stage_exec.completed_at = datetime.now(UTC)
        await _send_back_to_stage(
            run,
            stage_exec,
            target_index=target_index,
            target_stage=target_stage,
            gap_text=skill_return.outcome_reason or "",
            kickoff=kickoff,
            session=session,
        )
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
    # Mark every produced version final immediately rather than waiting on
    # a boundary decision — nothing reads a mid-loop artifact downstream
    # (the run engine only proceeds to the next stage once this one's whole
    # loop finishes), so there's no "wrong" version to guard against.
    await mark_final(artifact_id, session=session)
    return skill_return, artifact_id, artifact_body


async def _settle_stage_boundary(
    run: PipelineRunRow,
    stage: SkillStage | ReviewSkillStage,
    stage_exec: StageExecutionRow,
    *,
    kickoff: Kickoff,
    paths_affected: Sequence[str],
    stage_index: int,
    session: AsyncSession,
) -> None:
    """Shared boundary-evaluation tail for every stage kind that carries a
    `BoundaryControl` (`SkillStage`, `ReviewSkillStage`) once its own
    main/review work has settled — a main/fix `cannot_complete`/`send_back`
    outcome never reaches here (see `_validate_skill_return_and_artifact`),
    so the stage's own work has always "completed" by this point. A residual
    finding carrying a resolvable `defect_in_artifact` sends back before
    mode/conditions are ever evaluated (boundary order per architecture:
    send-back first, then conditions)."""
    residuals = [
        f for f in await list_for_stage_execution(stage_exec.id, session=session) if f.status == "open"
    ]

    flattened = FlattenedDefinition.from_snapshot(run.definition_snapshot)
    send_back = _resolve_residual_send_back(
        flattened, stage=stage, residuals=residuals, before_index=stage_index, run_id=run.id
    )
    if send_back is not None:
        target_index, target_stage, gap_text = send_back
        stage_exec.status = "completed"
        stage_exec.completed_at = datetime.now(UTC)
        await _send_back_to_stage(
            run,
            stage_exec,
            target_index=target_index,
            target_stage=target_stage,
            gap_text=gap_text,
            kickoff=kickoff,
            session=session,
        )
        return

    ticket = await get_ticket(run.ticket_id, org_id=run.org_id)
    assert stage_exec.confidence is not None
    decision = await evaluate_boundary(
        stage.boundary,
        org_id=run.org_id,
        repo_external_id=ticket.repo_external_id,
        residuals=residuals,
        paths_affected=paths_affected,
        confidence=stage_exec.confidence,  # type: ignore[arg-type]
        session=session,
    )

    stage_exec.status = "completed"
    stage_exec.completed_at = datetime.now(UTC)

    if decision.outcome == "pause":
        await _enter_pause(run, stage_exec, decision, kickoff=kickoff, session=session)
        return

    stage_exec.boundary_outcome = "proceeded"
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


async def _handle_main_return(
    run: PipelineRunRow,
    stage: SkillStage,
    stage_exec: StageExecutionRow,
    *,
    kickoff: Kickoff,
    outputs: dict[str, Any],
    session: AsyncSession,
) -> None:
    stage_index = stage_exec.stage_index
    result = await _validate_skill_return_and_artifact(
        run, stage, stage_exec, stage_index=stage_index, kickoff=kickoff, outputs=outputs, session=session
    )
    if result is None:
        return
    skill_return, artifact_id, artifact_body = result

    stage_exec.loop_state = [
        *stage_exec.loop_state,
        {
            "phase": "main",
            "artifact_id": str(artifact_id),
            "confidence": skill_return.confidence,
            "paths_affected": skill_return.paths_affected,
        },
    ]
    stage_exec.confidence = bucket_confidence(skill_return.confidence)
    _publish_artifact_stored(session, run)

    if stage.review is None:
        await _settle_stage_boundary(
            run,
            stage,
            stage_exec,
            kickoff=kickoff,
            paths_affected=skill_return.paths_affected,
            stage_index=stage_index,
            session=session,
        )
        return

    await _dispatch_review_invocation(run, stage, stage_exec, artifact_body=artifact_body, session=session)


async def _handle_fix_return(
    run: PipelineRunRow,
    stage: SkillStage,
    stage_exec: StageExecutionRow,
    *,
    kickoff: Kickoff,
    outputs: dict[str, Any],
    session: AsyncSession,
) -> None:
    stage_index = stage_exec.stage_index
    result = await _validate_skill_return_and_artifact(
        run, stage, stage_exec, stage_index=stage_index, kickoff=kickoff, outputs=outputs, session=session
    )
    if result is None:
        return
    skill_return, artifact_id, artifact_body = result

    stage_exec.loop_state = [
        *stage_exec.loop_state,
        {
            "phase": "fix",
            "artifact_id": str(artifact_id),
            "confidence": skill_return.confidence,
            "paths_affected": skill_return.paths_affected,
        },
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
            "verdicts": [
                {"finding_id": str(v.finding_id), "status": v.status, "reply": v.reply}
                for v in review_return.prior_finding_verdicts
            ],
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
    await _settle_stage_boundary(
        run,
        stage,
        stage_exec,
        kickoff=kickoff,
        paths_affected=_last_paths_affected(stage_exec),
        stage_index=stage_index,
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
    kickoff = Kickoff.model_validate(run.kickoff)

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
            "verdicts": [
                {"finding_id": str(v.finding_id), "status": v.status, "reply": v.reply}
                for v in review_return.prior_finding_verdicts
            ],
        },
    ]
    stage_exec.confidence = bucket_confidence(review_return.confidence)
    # `paths_affected` is always empty here — `SkillReviewReturn` carries no
    # such field, so `on_protected_code` can never trip for a standalone
    # review stage.
    await _settle_stage_boundary(
        run, stage, stage_exec, kickoff=kickoff, paths_affected=(), stage_index=stage_index, session=session
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
