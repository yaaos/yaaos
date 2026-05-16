"""Per-PR queue discipline + the review-job runner."""

from __future__ import annotations

import hashlib
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
from app.domain.vcs import Review
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


# Audit payloads


class _ScheduledPayload(BaseModel):
    trigger_reason: str
    agent_id: UUID
    debounce_seconds: int


class _CancelledPayload(BaseModel):
    reason: str


class _PromptSentPayload(BaseModel):
    agent_name: str
    prompt_hash: str
    lessons_count: int
    checkout_sha: str


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
    debounce = get_settings().yaaof_review_debounce_seconds

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

        spawn(
            f"review_job:{new_id}",
            _run_review_job(
                ReviewJobInput(
                    review_job_id=new_id,
                    ticket_id=ticket_id,
                    agent_id=agent.id,
                    org_id=org_id,
                    debounce_seconds=debounce,
                )
            ),
        )
        new_ids.append(new_id)
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


async def _run_review_job(input: ReviewJobInput) -> None:
    import asyncio  # noqa: PLC0415

    org_id = input.org_id
    job_id = input.review_job_id

    if input.debounce_seconds > 0:
        await asyncio.sleep(input.debounce_seconds)

    # Bail check after debounce
    async with db_session() as s:
        row = (await s.execute(select(ReviewJobRow).where(ReviewJobRow.id == job_id))).scalar_one_or_none()
    if row is None or row.status != "queued":
        return

    # Flip to running
    async with db_session() as s:
        await s.execute(
            update(ReviewJobRow)
            .where(ReviewJobRow.id == job_id)
            .values(
                status="running",
                started_at=_utcnow(),
                last_heartbeat_at=_utcnow(),
                current_step="resolving_entities",
            )
        )
        await s.commit()

    try:
        ticket = await tickets.get(input.ticket_id, org_id=org_id)
        if ticket.pr_id is None:
            await _transition_failed(job_id, "ticket has no linked PR", org_id=org_id)
            return
        pr = await pull_requests.get(ticket.pr_id, org_id=org_id)
        agent = await get_agent_by_id(input.agent_id, org_id=org_id)
        lessons = await memory.list_for_repo(pr.repo_external_id, org_id=org_id, plugin_id=pr.plugin_id)

        vcs_plugin = get_vcs_plugin(pr.plugin_id)
        diff = await vcs_plugin.fetch_diff(pr.external_id)
        prior_comments = await vcs_plugin.list_yaaof_comments(pr.external_id)
        prior_bodies = [c.body for c in prior_comments]

        # Skip checks
        if pr.is_fork:
            await _transition_skipped(job_id, "fork", org_id=org_id)
            return
        if pr.author_type == "bot":
            await _transition_skipped(job_id, "bot_author", org_id=org_id)
            return
        if diff.files and all(_is_skip_path(f.path) for f in diff.files):
            await _transition_skipped(job_id, "trivial_diff", org_id=org_id)
            return
        total_lines = sum(f.additions + f.deletions for f in diff.files)
        if total_lines > 5000:
            await _transition_skipped(job_id, "too_large", org_id=org_id)
            return

        # Language autodetected per review (was previously cached on repos.language_hint
        # — that column went away with the repos table). Cost is negligible: filename
        # extension scan over the diff's file list.
        language = _detect_language(diff)

        # Build the review context — the plugin owns prompt assembly.
        vcs_pr = await vcs_plugin.fetch_pr(pr.external_id)
        review_ctx = ReviewContext(
            persona=agent.prompt_text,
            agent_name=agent.name,
            pr=vcs_pr,
            diff=diff,
            lessons=lessons,
            language_hint=language,
            prior_yaaof_comment_bodies=prior_bodies,
            agent_config=agent.agent_config,
        )
        # Hash captures everything that influences the agent's output — same
        # purpose as the old assembled-prompt hash, just over the structured
        # context rather than the literal string.
        prompt_hash = hashlib.sha256(review_ctx.model_dump_json().encode()).hexdigest()
        lesson_ids = [lesson.id for lesson in lessons]

        async with db_session() as s:
            await s.execute(
                update(ReviewJobRow)
                .where(ReviewJobRow.id == job_id)
                .values(
                    prompt_hash=prompt_hash,
                    lessons_applied=lesson_ids,
                    current_step="invoking_agent",
                )
            )
            await s.commit()

        await audit_for_review_job(
            job_id,
            "review_job.prompt_sent",
            _PromptSentPayload(
                agent_name=agent.name,
                prompt_hash=prompt_hash,
                lessons_count=len(lessons),
                checkout_sha=pr.head_sha,
            ),
            actor=Actor.system(),
            org_id=org_id,
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
            # last cancel check before expensive call
            async with db_session() as s:
                row = (
                    await s.execute(select(ReviewJobRow).where(ReviewJobRow.id == job_id))
                ).scalar_one_or_none()
            if row is None or row.status != "running":
                return

            result = await coding_agent.review(
                plugin_id=agent.coding_agent_plugin_id,
                workspace=ws,
                context=review_ctx,
            )

        if result.status == InvocationStatus.SUCCESS:
            review_obj = Review(
                agent_tag=agent.name,
                state=result.state or "COMMENT",
                summary_body=result.summary_body,
                findings=result.findings,
            )
            post_result = await vcs_plugin.post_review(pr.external_id, review_obj)
            async with db_session() as s:
                # Record posted comments
                for cid in post_result.finding_to_comment_external_id.values():
                    s.add(
                        PostedCommentRow(
                            external_comment_id=cid,
                            org_id=org_id,
                            pr_id=pr.id,
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
                    ticket_id=input.ticket_id,
                    pr_id=pr.id,
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
        log.exception("review_job.handler_crashed", review_job_id=str(job_id))
        await _transition_failed(job_id, f"handler crashed: {e}", org_id=org_id, invocation_status="crashed")


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
    """Mark any running jobs from a prior process as failed; respawn queued jobs."""
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
                error="yaaof restarted during execution",
                raw_output_excerpt="",
            ),
            actor=Actor.system(),
            org_id=M01_ORG_ID,
        )

    for row in queued:
        # Resolve ticket_id via the PR row
        async with db_session() as s:
            from app.domain.pull_requests.models import PullRequestRow  # noqa: PLC0415

            pr_row = (
                await s.execute(select(PullRequestRow).where(PullRequestRow.id == row.pr_id))
            ).scalar_one_or_none()
        if pr_row is None:
            continue
        spawn(
            f"review_job:{row.id}",
            _run_review_job(
                ReviewJobInput(
                    review_job_id=row.id,
                    ticket_id=pr_row.ticket_id,
                    agent_id=row.agent_id,
                    org_id=row.org_id,
                    debounce_seconds=0,
                )
            ),
        )
