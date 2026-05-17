"""Per-PR queue discipline + the review-job runner."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel
from sqlalchemy import select, update

from app.core.audit_log import audit_for_review_job
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.events import Event, publish
from app.core.primitives import Actor, spawn
from app.core.workspace import (
    NetworkPolicy,
    RepoRefForSpec,
    ResourceCaps,
    Workspace,
    WorkspaceSpec,
    with_workspace,
)
from app.domain import coding_agent, memory, pull_requests, tickets
from app.domain.coding_agent import (
    InvocationStatus,
    ReplyContext,
    ReviewContext,
)
from app.domain.reviewer.agent_crud import get_agent_by_id, get_agent_by_name
from app.domain.reviewer.models import PostedCommentRow, ReviewJobRow
from app.domain.vcs import Diff, Review, VCSPullRequest
from app.domain.vcs import (
    get_plugin as get_vcs_plugin,
)

log = structlog.get_logger("reviewer")


M01_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


# Events


class ReviewJobStatusChanged(Event):
    kind: Literal["review_job_status_changed"] = "review_job_status_changed"
    source_module: Literal["reviewer"] = "reviewer"
    pr_id: UUID
    agent_id: UUID
    review_job_id: UUID
    status: str


class ReviewJobStepProgress(Event):
    """In-place row update — not an audit entry. Drives the running-state UI
    so operators can see which phase a long-running review is in.
    """

    kind: Literal["review_job_step_progress"] = "review_job_step_progress"
    source_module: Literal["reviewer"] = "reviewer"
    pr_id: UUID
    agent_id: UUID
    review_job_id: UUID
    current_step: str


# Audit payloads


class _ScheduledPayload(BaseModel):
    trigger_reason: str
    agent_id: UUID
    debounce_seconds: int


class _CancelledPayload(BaseModel):
    reason: str


class _AgentSnapshot(BaseModel):
    id: UUID
    name: str
    prompt_text: str
    coding_agent_plugin_id: str
    agent_config: dict[str, Any]


class _PromptSentPayload(BaseModel):
    """Frozen snapshot of everything that influenced this review.

    Captures the agent definition (so prompt-text rewrites later can't
    silently change what older audit entries refer to) alongside the
    content-derived hash and lesson context.
    """

    agent: _AgentSnapshot
    prompt_hash: str
    lessons_count: int
    lessons_applied: list[UUID]
    checkout_sha: str
    language_hint: str | None = None


class _PostedPayload(BaseModel):
    verdict: str
    finding_count: int
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: str | None
    latency_ms: int
    review_external_id: str


class _FailedPayload(BaseModel):
    invocation_status: str
    error: str | None
    raw_output_excerpt: str


class _SkippedPayload(BaseModel):
    skip_reason: str


class _ReplyPostedPayload(BaseModel):
    comment_external_id: str
    parent_comment_external_id: str
    tokens_in: int | None
    tokens_out: int | None


# ── Public API ────────────────────────────────────────────────────────────────


class ReviewJobInput(BaseModel):
    review_job_id: UUID
    ticket_id: UUID
    agent_id: UUID
    org_id: UUID
    debounce_seconds: int
    kind: Literal["review", "reply"] = "review"
    parent_comment_external_id: str | None = None
    reply_body: str | None = None


async def schedule_review(
    ticket_id: UUID,
    *,
    agent_names: Literal["all"] | list[str],
    trigger_reason: str,
    actor: Actor,
    org_id: UUID,
) -> list[UUID]:
    ticket = await tickets.get(ticket_id, org_id=org_id)
    if ticket.pr_id is None:
        return []
    pr = await pull_requests.get(ticket.pr_id, org_id=org_id)
    target_names: list[str]
    if agent_names == "all":
        target_names = ["architecture", "security", "style"]
    else:
        target_names = list(agent_names)
    debounce = get_settings().yaaos_review_debounce_seconds

    new_ids: list[UUID] = []
    for name in target_names:
        try:
            agent = await get_agent_by_name(name, org_id=org_id)
        except Exception:
            log.warning("reviewer.schedule_unknown_agent", name=name)
            continue

        # Cancel any in-flight job for (pr_id, agent_id)
        async with db_session() as s:
            inflight = (
                (
                    await s.execute(
                        select(ReviewJobRow).where(
                            ReviewJobRow.pr_id == pr.id,
                            ReviewJobRow.agent_id == agent.id,
                            ReviewJobRow.status.in_(["queued", "running"]),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in inflight:
                await s.execute(
                    update(ReviewJobRow)
                    .where(ReviewJobRow.id == row.id)
                    .values(
                        status="cancelled",
                        skip_reason="superseded",
                        completed_at=_utcnow(),
                    )
                )
            await s.commit()
            for row in inflight:
                await audit_for_review_job(
                    row.id,
                    "review_job.cancelled",
                    _CancelledPayload(reason="superseded"),
                    actor=actor,
                    org_id=org_id,
                )

        # Create the new queued row
        new_id = uuid4()
        async with db_session() as s:
            s.add(
                ReviewJobRow(
                    id=new_id,
                    org_id=org_id,
                    pr_id=pr.id,
                    agent_id=agent.id,
                    kind="review",
                    status="queued",
                )
            )
            await s.commit()
        await audit_for_review_job(
            new_id,
            "review_job.scheduled",
            _ScheduledPayload(
                trigger_reason=trigger_reason,
                agent_id=agent.id,
                debounce_seconds=debounce,
            ),
            actor=actor,
            org_id=org_id,
        )
        await publish(
            ReviewJobStatusChanged(
                ticket_id=ticket_id,
                pr_id=pr.id,
                agent_id=agent.id,
                review_job_id=new_id,
                status="queued",
            )
        )
        new_ids.append(new_id)

    # Spawn ONE coordinator per ticket. The coordinator provisions a single
    # workspace and runs every agent against it — the workspace belongs to
    # the ticket, not to individual review jobs. See `_run_ticket_review`.
    if new_ids:
        spawn(
            f"ticket_review:{ticket_id}",
            _run_ticket_review(
                ticket_id=ticket_id,
                job_ids=new_ids,
                debounce_seconds=debounce,
                org_id=org_id,
            ),
        )
    return new_ids


async def schedule_reply(
    ticket_id: UUID,
    agent_id: UUID,
    parent_comment_external_id: str,
    reply_body: str,
    *,
    actor: Actor,
    org_id: UUID,
) -> UUID:
    ticket = await tickets.get(ticket_id, org_id=org_id)
    if ticket.pr_id is None:
        raise ValueError("ticket has no linked PR")
    pr = await pull_requests.get(ticket.pr_id, org_id=org_id)

    # Supersede any in-flight reply for (pr, agent, parent_comment_external_id)
    async with db_session() as s:
        inflight = (
            (
                await s.execute(
                    select(ReviewJobRow).where(
                        ReviewJobRow.pr_id == pr.id,
                        ReviewJobRow.agent_id == agent_id,
                        ReviewJobRow.kind == "reply",
                        ReviewJobRow.parent_comment_external_id == parent_comment_external_id,
                        ReviewJobRow.status.in_(["queued", "running"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in inflight:
            await s.execute(
                update(ReviewJobRow)
                .where(ReviewJobRow.id == row.id)
                .values(status="cancelled", skip_reason="superseded", completed_at=_utcnow())
            )
        await s.commit()

    new_id = uuid4()
    async with db_session() as s:
        s.add(
            ReviewJobRow(
                id=new_id,
                org_id=org_id,
                pr_id=pr.id,
                agent_id=agent_id,
                kind="reply",
                status="queued",
                parent_comment_external_id=parent_comment_external_id,
                reply_body=reply_body,
            )
        )
        await s.commit()
    await audit_for_review_job(
        new_id,
        "review_job.scheduled",
        _ScheduledPayload(trigger_reason="reply", agent_id=agent_id, debounce_seconds=0),
        actor=actor,
        org_id=org_id,
    )
    spawn(
        f"reply_job:{new_id}",
        _run_reply_job(
            ReviewJobInput(
                review_job_id=new_id,
                ticket_id=ticket_id,
                agent_id=agent_id,
                org_id=org_id,
                debounce_seconds=0,
                kind="reply",
                parent_comment_external_id=parent_comment_external_id,
                reply_body=reply_body,
            )
        ),
    )
    return new_id


async def cancel_pending(
    ticket_id: UUID, *, actor: Actor, org_id: UUID, reason: str = "ticket_closed"
) -> int:
    ticket = await tickets.get(ticket_id, org_id=org_id)
    if ticket.pr_id is None:
        return 0
    async with db_session() as s:
        inflight = (
            (
                await s.execute(
                    select(ReviewJobRow).where(
                        ReviewJobRow.pr_id == ticket.pr_id,
                        ReviewJobRow.status.in_(["queued", "running"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in inflight:
            await s.execute(
                update(ReviewJobRow)
                .where(ReviewJobRow.id == row.id)
                .values(status="cancelled", skip_reason=reason, completed_at=_utcnow())
            )
        await s.commit()
    for row in inflight:
        await audit_for_review_job(
            row.id,
            "review_job.cancelled",
            _CancelledPayload(reason=reason),
            actor=actor,
            org_id=org_id,
        )
    return len(inflight)


# ── Read API ──────────────────────────────────────────────────────────────────


class ReviewJob(BaseModel):
    id: UUID
    org_id: UUID
    pr_id: UUID
    agent_id: UUID
    kind: str
    status: str
    skip_reason: str | None
    scheduled_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    last_heartbeat_at: datetime | None
    current_step: str | None
    prompt_hash: str | None
    lessons_applied: list[UUID] | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    duration_s: int | None
    error_message: str | None
    review_external_id: str | None
    findings: list[dict[str, Any]] | None

    @classmethod
    def from_row(cls, row: ReviewJobRow) -> ReviewJob:
        return cls(
            id=row.id,
            org_id=row.org_id,
            pr_id=row.pr_id,
            agent_id=row.agent_id,
            kind=row.kind,
            status=row.status,
            skip_reason=row.skip_reason,
            scheduled_at=row.scheduled_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            last_heartbeat_at=row.last_heartbeat_at,
            current_step=row.current_step,
            prompt_hash=row.prompt_hash,
            lessons_applied=row.lessons_applied,
            tokens_in=row.tokens_in,
            tokens_out=row.tokens_out,
            cost_usd=float(row.cost_usd) if row.cost_usd is not None else None,
            duration_s=row.duration_s,
            error_message=row.error_message,
            review_external_id=row.review_external_id,
            findings=row.findings,
        )


async def get_review_job(review_job_id: UUID, *, org_id: UUID) -> ReviewJob:
    async with db_session() as s:
        row = (
            await s.execute(
                select(ReviewJobRow).where(ReviewJobRow.id == review_job_id, ReviewJobRow.org_id == org_id)
            )
        ).scalar_one_or_none()
    if row is None:
        raise LookupError(str(review_job_id))
    return ReviewJob.from_row(row)


async def list_review_jobs_for_pr(pr_id: UUID, *, org_id: UUID) -> list[ReviewJob]:
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(ReviewJobRow)
                    .where(ReviewJobRow.pr_id == pr_id, ReviewJobRow.org_id == org_id)
                    .order_by(ReviewJobRow.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    return [ReviewJob.from_row(r) for r in rows]


async def list_in_flight(*, org_id: UUID) -> list[ReviewJob]:
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(ReviewJobRow).where(
                        ReviewJobRow.org_id == org_id,
                        ReviewJobRow.status.in_(["queued", "running"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    return [ReviewJob.from_row(r) for r in rows]


async def metrics_summary(*, org_id: UUID) -> dict[str, Any]:
    """Aggregate counters for the basic-metrics requirement (done-means)."""
    async with db_session() as s:
        rows = (await s.execute(select(ReviewJobRow).where(ReviewJobRow.org_id == org_id))).scalars().all()
    statuses: dict[str, int] = {}
    total_cost = 0.0
    posted = 0
    failed = 0
    for r in rows:
        statuses[r.status] = statuses.get(r.status, 0) + 1
        if r.status == "posted":
            posted += 1
            if r.cost_usd is not None:
                total_cost += float(r.cost_usd)
        if r.status == "failed":
            failed += 1
    return {
        "review_jobs_by_status": statuses,
        "total_reviews_posted": posted,
        "total_cost_usd": round(total_cost, 4),
        "failure_count": failed,
        "failure_rate": (failed / (posted + failed)) if (posted + failed) > 0 else 0.0,
    }


# ── Handler ───────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _SharedReviewContext:
    """Inputs shared by every agent reviewing the same ticket.

    Resolved once per `_run_ticket_review` call so we don't re-fetch the PR,
    diff, prior comments, lessons, or recompute the language hint N times.
    Agent-specific context (persona, agent_config) is layered on per agent
    inside `_invoke_one_agent`.
    """

    ticket_id: UUID
    pr_id: UUID
    pr_external_id: str
    repo_external_id: str
    plugin_id: str
    vcs_pr: VCSPullRequest
    diff: Diff
    prior_bodies: list[str]
    lessons: list[Any]
    language: str | None


async def _run_ticket_review(
    *,
    ticket_id: UUID,
    job_ids: list[UUID],
    debounce_seconds: int,
    org_id: UUID,
) -> None:
    """Coordinator: one workspace per ticket, shared by every agent.

    Flow: debounce → drop already-cancelled jobs → fetch shared context once →
    apply ticket-level skip checks + secrets pre-flight (transitioning all
    pending jobs at once) → provision ONE workspace → run every agent in
    parallel against it via `asyncio.gather` → workspace closes when all agents
    return.

    Replaces the prior per-agent `_run_review_job` design where each agent
    provisioned its own workspace. Each ticket now gets a fully isolated
    workspace; the agents share it.
    """
    if debounce_seconds > 0:
        await asyncio.sleep(debounce_seconds)

    # Filter to still-queued jobs (others were cancelled or superseded
    # during the debounce window).
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(ReviewJobRow).where(
                        ReviewJobRow.id.in_(job_ids),
                        ReviewJobRow.status == "queued",
                    )
                )
            )
            .scalars()
            .all()
        )
    pending = list(rows)
    if not pending:
        return

    # Flip every pending job to running BEFORE doing any heavy work so the
    # UI reflects activity immediately + cancel-checks have something to see.
    async with db_session() as s:
        await s.execute(
            update(ReviewJobRow)
            .where(ReviewJobRow.id.in_([r.id for r in pending]))
            .values(
                status="running",
                started_at=_utcnow(),
                last_heartbeat_at=_utcnow(),
                current_step="resolving_entities",
            )
        )
        await s.commit()

    try:
        ticket = await tickets.get(ticket_id, org_id=org_id)
        if ticket.pr_id is None:
            for job in pending:
                await _transition_failed(job.id, "ticket has no linked PR", org_id=org_id)
            return
        pr = await pull_requests.get(ticket.pr_id, org_id=org_id)
        vcs_plugin = get_vcs_plugin(pr.plugin_id)

        for job in pending:
            await _set_step(job.id, "fetching_diff", ticket_id=ticket_id, pr_id=pr.id, agent_id=job.agent_id)

        lessons = await memory.list_for_repo(pr.repo_external_id, org_id=org_id, plugin_id=pr.plugin_id)
        diff = await vcs_plugin.fetch_diff(pr.external_id)
        prior_comments = await vcs_plugin.list_yaaos_comments(pr.external_id)
        prior_bodies = [c.body for c in prior_comments]
        vcs_pr = await vcs_plugin.fetch_pr(pr.external_id)

        # Ticket-level skip checks. A skip transitions ALL pending jobs
        # at once — there's no per-agent variation in these predicates.
        skip_reason = _ticket_skip_reason(pr, diff)
        if skip_reason is not None:
            for job in pending:
                await _transition_skipped(job.id, skip_reason, org_id=org_id)
            return

        # Secrets pre-flight: post ONE refusal review (not one per agent
        # like before) and transition every job to skipped.
        secret_rule = _detect_secrets(diff)
        if secret_rule is not None:
            try:
                # Tag the warning with the first pending agent's name; the
                # body explains the refusal independent of agent identity.
                first_agent = await get_agent_by_id(pending[0].agent_id, org_id=org_id)
                await vcs_plugin.post_review(
                    pr.external_id, _secrets_warning_review(first_agent.name, secret_rule)
                )
            except Exception:
                log.exception("ticket_review.secrets_warning_post_failed", ticket_id=str(ticket_id))
            for job in pending:
                await _transition_skipped(job.id, "secrets_detected", org_id=org_id)
            return

        language = _detect_language(diff)

        ctx = _SharedReviewContext(
            ticket_id=ticket_id,
            pr_id=pr.id,
            pr_external_id=pr.external_id,
            repo_external_id=pr.repo_external_id,
            plugin_id=pr.plugin_id,
            vcs_pr=vcs_pr,
            diff=diff,
            prior_bodies=prior_bodies,
            lessons=lessons,
            language=language,
        )

        for job in pending:
            await _set_step(
                job.id, "provisioning_workspace", ticket_id=ticket_id, pr_id=pr.id, agent_id=job.agent_id
            )

        # ONE workspace for the whole ticket. Agents run in parallel against
        # it via asyncio.gather — they share the checkout but each invokes
        # its own coding-agent subprocess. M01 agents are read-only against
        # the workspace; write isolation is an M02+ concern when implementer
        # agents land.
        async with with_workspace(
            "in_process",
            WorkspaceSpec(
                repo=RepoRefForSpec(plugin_id=pr.plugin_id, external_id=pr.repo_external_id),
                sha=pr.head_sha,
                branch_name=pr.head_branch,
                resource_caps=ResourceCaps(),
                network_policy=NetworkPolicy.GITHUB_ONLY,
                org_id=org_id,
            ),
            org_id=org_id,
        ) as ws:
            await asyncio.gather(
                *(_invoke_one_agent(workspace=ws, job_id=job.id, ctx=ctx, org_id=org_id) for job in pending),
                return_exceptions=False,
            )

    except Exception as e:
        log.exception("ticket_review.coordinator_crashed", ticket_id=str(ticket_id))
        # Any pending row still in `running` from this coordinator is
        # stranded — mark them failed so the UI doesn't show forever-running.
        async with db_session() as s:
            stuck = (
                (
                    await s.execute(
                        select(ReviewJobRow.id).where(
                            ReviewJobRow.id.in_([r.id for r in pending]),
                            ReviewJobRow.status == "running",
                        )
                    )
                )
                .scalars()
                .all()
            )
        for jid in stuck:
            await _transition_failed(
                jid, f"coordinator crashed: {e}", org_id=org_id, invocation_status="crashed"
            )


async def _invoke_one_agent(
    *,
    workspace: Workspace,
    job_id: UUID,
    ctx: _SharedReviewContext,
    org_id: UUID,
) -> None:
    """Run one agent against the shared workspace.

    Builds the per-agent `ReviewContext`, audits the frozen-snapshot
    `prompt_sent`, invokes `coding_agent.review`, posts the review, and
    updates the job row. Each invocation is independent — one agent failing
    doesn't affect the others, and the gather caller awaits all of them.
    """
    try:
        # Cancel check before doing any work — operator may have cancelled
        # the job between the coordinator flipping it to running and this
        # coroutine getting scheduled.
        async with db_session() as s:
            row = (
                await s.execute(select(ReviewJobRow).where(ReviewJobRow.id == job_id))
            ).scalar_one_or_none()
        if row is None or row.status != "running":
            return
        agent_id = row.agent_id

        agent = await get_agent_by_id(agent_id, org_id=org_id)
        review_ctx = ReviewContext(
            persona=agent.prompt_text,
            agent_name=agent.name,
            pr=ctx.vcs_pr,
            diff=ctx.diff,
            lessons=ctx.lessons,
            language_hint=ctx.language,
            prior_yaaos_comment_bodies=ctx.prior_bodies,
            agent_config=agent.agent_config,
        )
        prompt_hash = hashlib.sha256(review_ctx.model_dump_json().encode()).hexdigest()
        lesson_ids = [lesson.id for lesson in ctx.lessons]

        async with db_session() as s:
            await s.execute(
                update(ReviewJobRow)
                .where(ReviewJobRow.id == job_id)
                .values(prompt_hash=prompt_hash, lessons_applied=lesson_ids)
            )
            await s.commit()

        await audit_for_review_job(
            job_id,
            "review_job.prompt_sent",
            _PromptSentPayload(
                agent=_AgentSnapshot(
                    id=agent.id,
                    name=agent.name,
                    prompt_text=agent.prompt_text,
                    coding_agent_plugin_id=agent.coding_agent_plugin_id,
                    agent_config=agent.agent_config,
                ),
                prompt_hash=prompt_hash,
                lessons_count=len(ctx.lessons),
                lessons_applied=lesson_ids,
                checkout_sha=ctx.vcs_pr.head_sha,
                language_hint=ctx.language,
            ),
            actor=Actor.system(),
            org_id=org_id,
        )

        # Last cancel check before the expensive CLI call.
        async with db_session() as s:
            row = (
                await s.execute(select(ReviewJobRow).where(ReviewJobRow.id == job_id))
            ).scalar_one_or_none()
        if row is None or row.status != "running":
            return

        await _set_step(job_id, "invoking_agent", ticket_id=ctx.ticket_id, pr_id=ctx.pr_id, agent_id=agent.id)

        result = await coding_agent.review(
            plugin_id=agent.coding_agent_plugin_id,
            workspace=workspace,
            context=review_ctx,
        )

        if result.status == InvocationStatus.SUCCESS:
            await _set_step(
                job_id, "posting_review", ticket_id=ctx.ticket_id, pr_id=ctx.pr_id, agent_id=agent.id
            )
            review_obj = Review(
                agent_tag=agent.name,
                state=result.state or "COMMENT",
                summary_body=result.summary_body,
                findings=result.findings,
            )
            vcs_plugin = get_vcs_plugin(ctx.plugin_id)
            post_result = await vcs_plugin.post_review(ctx.pr_external_id, review_obj)
            async with db_session() as s:
                for cid in post_result.finding_to_comment_external_id.values():
                    s.add(
                        PostedCommentRow(
                            external_comment_id=cid,
                            org_id=org_id,
                            pr_id=ctx.pr_id,
                            review_job_id=job_id,
                            agent_id=agent.id,
                        )
                    )
                started = (
                    await s.execute(select(ReviewJobRow).where(ReviewJobRow.id == job_id))
                ).scalar_one_or_none()
                duration = None
                if started and started.started_at:
                    duration = int((_utcnow() - started.started_at).total_seconds())
                await s.execute(
                    update(ReviewJobRow)
                    .where(ReviewJobRow.id == job_id)
                    .values(
                        status="posted",
                        completed_at=_utcnow(),
                        review_external_id=post_result.review_external_id,
                        tokens_in=result.telemetry.tokens_in,
                        tokens_out=result.telemetry.tokens_out,
                        cost_usd=float(result.telemetry.cost_usd)
                        if result.telemetry.cost_usd is not None
                        else None,
                        duration_s=duration,
                        current_step="posted",
                        findings=[f.model_dump(mode="json") for f in result.findings],
                    )
                )
                await s.commit()
            await audit_for_review_job(
                job_id,
                "review_job.posted",
                _PostedPayload(
                    verdict=result.state or "COMMENT",
                    finding_count=len(result.findings),
                    tokens_in=result.telemetry.tokens_in,
                    tokens_out=result.telemetry.tokens_out,
                    cost_usd=str(result.telemetry.cost_usd)
                    if result.telemetry.cost_usd is not None
                    else None,
                    latency_ms=result.telemetry.latency_ms,
                    review_external_id=post_result.review_external_id,
                ),
                actor=Actor.agent(agent.id),
                org_id=org_id,
            )
            await publish(
                ReviewJobStatusChanged(
                    ticket_id=ctx.ticket_id,
                    pr_id=ctx.pr_id,
                    agent_id=agent.id,
                    review_job_id=job_id,
                    status="posted",
                )
            )
        else:
            await _transition_failed(
                job_id,
                result.error_message or f"agent returned status={result.status}",
                org_id=org_id,
                invocation_status=str(result.status),
                raw_output_excerpt=(result.telemetry.raw_output or "")[:1000],
            )

    except Exception as e:
        log.exception("invoke_one_agent.crashed", review_job_id=str(job_id))
        await _transition_failed(
            job_id, f"agent invocation crashed: {e}", org_id=org_id, invocation_status="crashed"
        )


def _ticket_skip_reason(pr: Any, diff: Diff) -> str | None:
    """Return a skip reason if the whole ticket should be skipped, else None.

    These checks are ticket-level — they don't vary by agent. Centralising
    them avoids posting three skip-audit entries with the same reason and
    keeps the coordinator's flow linear.
    """
    if pr.is_fork:
        return "fork"
    if pr.author_type == "bot":
        return "bot_author"
    if diff.files and all(_is_skip_path(f.path) for f in diff.files):
        return "trivial_diff"
    total_lines = sum(f.additions + f.deletions for f in diff.files)
    if total_lines > 5000:
        return "too_large"
    return None


async def _run_reply_job(input: ReviewJobInput) -> None:
    org_id = input.org_id
    job_id = input.review_job_id

    async with db_session() as s:
        await s.execute(
            update(ReviewJobRow)
            .where(ReviewJobRow.id == job_id)
            .values(status="running", started_at=_utcnow(), current_step="building_prompt")
        )
        await s.commit()
    try:
        ticket = await tickets.get(input.ticket_id, org_id=org_id)
        if ticket.pr_id is None:
            await _transition_failed(job_id, "no PR", org_id=org_id)
            return
        pr = await pull_requests.get(ticket.pr_id, org_id=org_id)
        agent = await get_agent_by_id(input.agent_id, org_id=org_id)
        vcs_plugin = get_vcs_plugin(pr.plugin_id)
        diff = await vcs_plugin.fetch_diff(pr.external_id)
        vcs_pr = await vcs_plugin.fetch_pr(pr.external_id)
        reply_ctx = ReplyContext(
            persona=agent.prompt_text,
            agent_name=agent.name,
            pr=vcs_pr,
            diff=diff,
            reply_body=input.reply_body or "",
            parent_comment_external_id=input.parent_comment_external_id or "",
            agent_config=agent.agent_config,
        )

        async with with_workspace(
            "in_process",
            WorkspaceSpec(
                repo=RepoRefForSpec(plugin_id=pr.plugin_id, external_id=pr.repo_external_id),
                sha=pr.head_sha,
                branch_name=pr.head_branch,
                resource_caps=ResourceCaps(),
                network_policy=NetworkPolicy.GITHUB_ONLY,
                org_id=org_id,
            ),
            org_id=org_id,
        ) as ws:
            result = await coding_agent.reply(
                plugin_id=agent.coding_agent_plugin_id,
                workspace=ws,
                context=reply_ctx,
            )

        if result.status == InvocationStatus.SUCCESS and result.body is not None:
            new_comment_id = await vcs_plugin.post_comment_reply(
                pr.external_id,
                input.parent_comment_external_id or "",
                f"[{agent.name}] {result.body}",
            )
            async with db_session() as s:
                await s.execute(
                    update(ReviewJobRow)
                    .where(ReviewJobRow.id == job_id)
                    .values(
                        status="posted",
                        completed_at=_utcnow(),
                        tokens_in=result.telemetry.tokens_in,
                        tokens_out=result.telemetry.tokens_out,
                        cost_usd=float(result.telemetry.cost_usd)
                        if result.telemetry.cost_usd is not None
                        else None,
                    )
                )
                await s.commit()
            await audit_for_review_job(
                job_id,
                "review_job.reply_posted",
                _ReplyPostedPayload(
                    comment_external_id=new_comment_id,
                    parent_comment_external_id=input.parent_comment_external_id or "",
                    tokens_in=result.telemetry.tokens_in,
                    tokens_out=result.telemetry.tokens_out,
                ),
                actor=Actor.agent(agent.id),
                org_id=org_id,
            )
        else:
            await _transition_failed(job_id, result.error_message or "agent_error", org_id=org_id)
    except Exception as e:
        log.exception("reply_job.handler_crashed", review_job_id=str(job_id))
        await _transition_failed(job_id, f"handler crashed: {e}", org_id=org_id)


async def _transition_failed(
    job_id: UUID,
    error: str,
    *,
    org_id: UUID,
    invocation_status: str = "agent_error",
    raw_output_excerpt: str = "",
) -> None:
    async with db_session() as s:
        await s.execute(
            update(ReviewJobRow)
            .where(ReviewJobRow.id == job_id)
            .values(
                status="failed",
                completed_at=_utcnow(),
                error_message=error,
                current_step="failed",
            )
        )
        await s.commit()
    await audit_for_review_job(
        job_id,
        "review_job.failed",
        _FailedPayload(
            invocation_status=invocation_status,
            error=error,
            raw_output_excerpt=raw_output_excerpt,
        ),
        actor=Actor.system(),
        org_id=org_id,
    )


async def _transition_skipped(job_id: UUID, reason: str, *, org_id: UUID) -> None:
    async with db_session() as s:
        await s.execute(
            update(ReviewJobRow)
            .where(ReviewJobRow.id == job_id)
            .values(status="skipped", skip_reason=reason, completed_at=_utcnow())
        )
        await s.commit()
    await audit_for_review_job(
        job_id,
        "review_job.skipped",
        _SkippedPayload(skip_reason=reason),
        actor=Actor.system(),
        org_id=org_id,
    )


def _is_skip_path(path: str) -> bool:
    from app.domain.intake.parsing import is_skippable_path  # noqa: PLC0415

    return is_skippable_path(path)


# Patterns for the pre-flight secrets check. Only the high-confidence shapes —
# anything entropy-only would false-positive at POC scale. Each match raises a
# refuse-to-review on the PR; the audit log captures *which rule* matched, never
# the secret itself.
_SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("private_key_pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


def _detect_secrets(diff: Diff) -> str | None:
    """Scan added lines in `diff.raw` for high-confidence secret shapes.

    Returns the rule id that matched first, or None if nothing matched.
    Only `+`-prefixed lines are scanned — `-` and context lines are pre-existing
    content and are out of scope for the pre-flight refuse-to-review check.
    """
    for raw_line in (diff.raw or "").splitlines():
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        for rule_id, pat in _SECRET_RULES:
            if pat.search(raw_line):
                return rule_id
    return None


def _secrets_warning_review(agent_name: str, rule_id: str) -> Review:
    """One-shot review posted when the secrets pre-flight refuses to proceed."""
    body = (
        "yaaos refused to review this PR — the diff contains content that "
        f"looks like a leaked secret (rule: `{rule_id}`). Remove the secret, "
        "rotate it on the upstream provider, then push a fresh commit and the "
        "review will run automatically."
    )
    return Review(agent_tag=agent_name, state="COMMENT", summary_body=body, findings=[])


async def _set_step(
    job_id: UUID,
    step: str,
    *,
    ticket_id: UUID,
    pr_id: UUID,
    agent_id: UUID,
) -> None:
    """Update `current_step` + heartbeat and publish a step-progress event.

    Step changes do NOT generate audit entries (M01 decision: heartbeat noise
    isn't worth durable history). The SSE event drives the UI; the column
    drives polling fallback.
    """
    async with db_session() as s:
        await s.execute(
            update(ReviewJobRow)
            .where(ReviewJobRow.id == job_id)
            .values(current_step=step, last_heartbeat_at=_utcnow())
        )
        await s.commit()
    await publish(
        ReviewJobStepProgress(
            ticket_id=ticket_id,
            pr_id=pr_id,
            agent_id=agent_id,
            review_job_id=job_id,
            current_step=step,
        )
    )


def _detect_language(diff: Any) -> str | None:
    ext_to_lang = {
        ".py": "Python",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".go": "Go",
        ".rs": "Rust",
        ".rb": "Ruby",
        ".java": "Java",
        ".kt": "Kotlin",
        ".swift": "Swift",
        ".c": "C",
        ".cpp": "C++",
        ".cc": "C++",
        ".h": "C/C++",
    }
    counts: dict[str, int] = {}
    for f in diff.files:
        for ext, lang in ext_to_lang.items():
            if f.path.lower().endswith(ext):
                counts[lang] = counts.get(lang, 0) + 1
                break
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ── Startup recovery ──────────────────────────────────────────────────────────


async def startup_recovery() -> None:
    """Mark any running jobs from a prior process as failed; respawn queued
    jobs grouped by ticket (one coordinator per ticket, not per job).
    """
    from app.domain.pull_requests.models import PullRequestRow  # noqa: PLC0415

    async with db_session() as s:
        crashed = (
            (await s.execute(select(ReviewJobRow.id).where(ReviewJobRow.status == "running"))).scalars().all()
        )
        if crashed:
            await s.execute(
                update(ReviewJobRow)
                .where(ReviewJobRow.status == "running")
                .values(
                    status="failed",
                    skip_reason="crashed",
                    completed_at=_utcnow(),
                    error_message="process crashed mid-execution",
                )
            )
        queued = (
            (await s.execute(select(ReviewJobRow).where(ReviewJobRow.status == "queued"))).scalars().all()
        )
        await s.commit()

    for jid in crashed:
        await audit_for_review_job(
            jid,
            "review_job.failed",
            _FailedPayload(
                invocation_status="crashed",
                error="yaaos restarted during execution",
                raw_output_excerpt="",
            ),
            actor=Actor.system(),
            org_id=M01_ORG_ID,
        )

    # Group queued jobs by ticket_id (resolved via the PR row) so one
    # coordinator respawns per ticket — matching the ticket-scoped workspace
    # discipline used by `schedule_review`.
    by_ticket: dict[UUID, dict[str, Any]] = {}
    for row in queued:
        async with db_session() as s:
            pr_row = (
                await s.execute(select(PullRequestRow).where(PullRequestRow.id == row.pr_id))
            ).scalar_one_or_none()
        if pr_row is None:
            continue
        entry = by_ticket.setdefault(pr_row.ticket_id, {"job_ids": [], "org_id": row.org_id})
        entry["job_ids"].append(row.id)

    for ticket_id, entry in by_ticket.items():
        spawn(
            f"ticket_review:{ticket_id}",
            _run_ticket_review(
                ticket_id=ticket_id,
                job_ids=entry["job_ids"],
                debounce_seconds=0,
                org_id=entry["org_id"],
            ),
        )
