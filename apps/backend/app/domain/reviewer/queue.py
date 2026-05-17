"""Per-PR queue discipline + the review-job runner.

One review job per (PR x review run). The runner provisions a workspace, calls
the coding agent (which dispatches yaaos-* subagents internally and synthesizes
their findings), and posts one Review to the VCS.

Reply / verify-fix flows are deferred — a future `review_comments` table will
own that lifecycle separately. For now no reply path exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
    WorkspaceSpec,
    with_workspace,
)
from app.domain import coding_agent, memory, pull_requests, tickets
from app.domain.coding_agent import ActivityEvent, InvocationStatus, ReviewContext
from app.domain.reviewer.models import PostedCommentRow, ReviewJobRow
from app.domain.vcs import Diff, Review, VCSPullRequest
from app.domain.vcs import (
    get_plugin as get_vcs_plugin,
)

log = structlog.get_logger("reviewer")


M01_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

# Hard-coded reviewer identity. There's only one reviewer (the parent agent
# that dispatches subagents); we use this tag on the top-level GitHub review
# body. Per-comment prefixes come from each finding's `source_agent` field.
_REVIEWER_TAG = "yaaos"
_CODING_AGENT_PLUGIN_ID = "claude_code"

# Recorded onto every review_jobs row at insert time. Mirrors the constants
# in `plugins/claude_code` (`_MODEL`, `_EFFORT`). Duplicated to keep the
# Tach layering clean — `domain/reviewer` cannot import from `plugins/*`.
# Future UI configuration replaces both copies with a settings row.
_DEFAULT_MODEL = "opus"
_DEFAULT_EFFORT = "medium"


# In-flight task registry, keyed by review_job_id. Used by `cancel_pending`
# to interrupt the running coro mid-CLI: flipping the DB row to cancelled
# alone leaves the subprocess running until its own timeout. Cancelling the
# asyncio task propagates `CancelledError` down through `coding_agent.review`
# → `workspace.run_coding_agent_cli`, which kills the subprocess group
# (SIGTERM → 2s → SIGKILL) before the cancellation unwinds further. The
# done callback removes entries automatically; this registry is best-effort
# (only useful for tasks spawned in the current process — see `startup_recovery`
# for the cross-restart story).
_inflight_tasks: dict[UUID, asyncio.Task[None]] = {}


def _register_inflight(job_id: UUID, task: asyncio.Task[None]) -> None:
    _inflight_tasks[job_id] = task
    task.add_done_callback(lambda _t: _inflight_tasks.pop(job_id, None))


# Events


class ReviewJobStatusChanged(Event):
    kind: Literal["review_job_status_changed"] = "review_job_status_changed"
    source_module: Literal["reviewer"] = "reviewer"
    pr_id: UUID
    review_job_id: UUID
    status: str


class ReviewJobStepProgress(Event):
    """In-place row update — not an audit entry. Drives the running-state UI."""

    kind: Literal["review_job_step_progress"] = "review_job_step_progress"
    source_module: Literal["reviewer"] = "reviewer"
    pr_id: UUID
    review_job_id: UUID
    current_step: str


class ReviewJobActivity(Event):
    """One captured stream event from the coding-agent CLI.

    High-frequency (~50-100 per review). Not persisted as an audit entry —
    the per-row `activity_log` JSONB column carries the durable copy. SSE
    consumers push events into a local store keyed by review_job_id.
    """

    kind: Literal["review_job_activity"] = "review_job_activity"
    source_module: Literal["reviewer"] = "reviewer"
    pr_id: UUID
    review_job_id: UUID
    event: dict[str, Any]


# Audit payloads


class _ScheduledPayload(BaseModel):
    trigger_reason: str
    debounce_seconds: int


class _CancelledPayload(BaseModel):
    reason: str


class _PromptSentPayload(BaseModel):
    """Frozen snapshot of what influenced this review run."""

    prompt_hash: str
    lessons_count: int
    lessons_applied: list[UUID]
    checkout_sha: str
    language_hint: str | None = None


class _PostedPayload(BaseModel):
    verdict: str
    finding_count: int
    findings_by_agent: dict[str, int]
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int
    review_external_id: str


class _FailedPayload(BaseModel):
    invocation_status: str
    error: str | None
    raw_output_excerpt: str


class _SkippedPayload(BaseModel):
    skip_reason: str


# ── Public API ────────────────────────────────────────────────────────────────


class ReviewJobInput(BaseModel):
    review_job_id: UUID
    ticket_id: UUID
    org_id: UUID
    debounce_seconds: int


async def schedule_review(
    ticket_id: UUID,
    *,
    trigger_reason: str,
    actor: Actor,
    org_id: UUID,
) -> UUID | None:
    """Schedule one review run for the ticket's PR.

    Cancels any in-flight job for the same PR (superseded). Returns the new
    job id, or None if the ticket has no PR.
    """
    ticket = await tickets.get(ticket_id, org_id=org_id)
    if ticket.pr_id is None:
        return None
    pr = await pull_requests.get(ticket.pr_id, org_id=org_id)
    debounce = get_settings().yaaos_review_debounce_seconds

    # Cancel any in-flight job for this PR.
    async with db_session() as s:
        inflight = (
            (
                await s.execute(
                    select(ReviewJobRow).where(
                        ReviewJobRow.pr_id == pr.id,
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
        for row in inflight:
            await audit_for_review_job(
                row.id,
                "review_job.cancelled",
                _CancelledPayload(reason="superseded"),
                actor=actor,
                org_id=org_id,
            )

    new_id = uuid4()
    async with db_session() as s:
        s.add(
            ReviewJobRow(
                id=new_id,
                org_id=org_id,
                pr_id=pr.id,
                status="queued",
                triggered_by=trigger_reason,
                destination="vcs",
                model=_DEFAULT_MODEL,
                effort=_DEFAULT_EFFORT,
            )
        )
        await s.commit()
    await audit_for_review_job(
        new_id,
        "review_job.scheduled",
        _ScheduledPayload(trigger_reason=trigger_reason, debounce_seconds=debounce),
        actor=actor,
        org_id=org_id,
    )
    await publish(ReviewJobStatusChanged(pr_id=pr.id, review_job_id=new_id, status="queued"))
    task = spawn(
        f"review_job:{new_id}",
        _run_review_job(
            ReviewJobInput(
                review_job_id=new_id,
                ticket_id=ticket_id,
                org_id=org_id,
                debounce_seconds=debounce,
            )
        ),
    )
    _register_inflight(new_id, task)
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
        # Interrupt the in-flight coro so the CLI subprocess is actually
        # killed — not just left running until its own timeout. The asyncio
        # cancel propagates through `coding_agent.review` →
        # `workspace.run_coding_agent_cli`, which catches `CancelledError`
        # and kills the subprocess group before the cancellation unwinds.
        # If no task is registered (e.g., post-restart), this is a no-op;
        # the DB row is already in `cancelled` and that's what the UI shows.
        task = _inflight_tasks.get(row.id)
        if task is not None and not task.done():
            task.cancel()
    return len(inflight)


# ── Read API ──────────────────────────────────────────────────────────────────


class ReviewJob(BaseModel):
    id: UUID
    org_id: UUID
    pr_id: UUID
    status: str
    triggered_by: str
    destination: str
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
    duration_s: int | None
    error_message: str | None
    review_external_id: str | None
    findings: list[dict[str, Any]] | None
    activity_log: list[dict[str, Any]]
    model: str | None
    effort: str | None

    @classmethod
    def from_row(cls, row: ReviewJobRow) -> ReviewJob:
        return cls(
            id=row.id,
            org_id=row.org_id,
            pr_id=row.pr_id,
            status=row.status,
            triggered_by=row.triggered_by,
            destination=row.destination,
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
            duration_s=row.duration_s,
            error_message=row.error_message,
            review_external_id=row.review_external_id,
            findings=row.findings,
            activity_log=row.activity_log or [],
            model=row.model,
            effort=row.effort,
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
    """Aggregate counters for the basic-metrics requirement."""
    async with db_session() as s:
        rows = (await s.execute(select(ReviewJobRow).where(ReviewJobRow.org_id == org_id))).scalars().all()
    statuses: dict[str, int] = {}
    posted = 0
    failed = 0
    for r in rows:
        statuses[r.status] = statuses.get(r.status, 0) + 1
        if r.status == "posted":
            posted += 1
        if r.status == "failed":
            failed += 1
    return {
        "review_jobs_by_status": statuses,
        "total_reviews_posted": posted,
        "failure_count": failed,
        "failure_rate": (failed / (posted + failed)) if (posted + failed) > 0 else 0.0,
    }


# ── Handler ───────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _ResolvedContext:
    """Inputs resolved once per review run."""

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


async def _run_review_job(input: ReviewJobInput) -> None:
    """Run one review job end-to-end: provision workspace, invoke parent
    reviewer, post one Review to the VCS.
    """
    job_id = input.review_job_id
    ticket_id = input.ticket_id
    org_id = input.org_id

    if input.debounce_seconds > 0:
        await asyncio.sleep(input.debounce_seconds)

    # Bail if the job was cancelled during the debounce window.
    async with db_session() as s:
        row = (await s.execute(select(ReviewJobRow).where(ReviewJobRow.id == job_id))).scalar_one_or_none()
    if row is None or row.status != "queued":
        return

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
    await publish(ReviewJobStatusChanged(pr_id=row.pr_id, review_job_id=job_id, status="running"))

    # Activity buffer for this review run. Each entry is a dict-serialised
    # `ActivityEvent`. Capped at ~5 MB total; once exceeded, append a
    # `log_truncated` marker and stop persisting (live SSE still flows so the
    # UI keeps updating). Persisted on every terminal transition below.
    activity_buffer: list[dict[str, Any]] = []
    activity_bytes = 0
    activity_truncated = False
    activity_cap_bytes = 5 * 1024 * 1024

    async def _on_activity(event: ActivityEvent) -> None:
        nonlocal activity_bytes, activity_truncated
        entry = event.model_dump(mode="json")
        if not activity_truncated:
            entry_size = len(json.dumps(entry))
            if activity_bytes + entry_size > activity_cap_bytes:
                activity_buffer.append(
                    {
                        "ts": event.ts.isoformat(),
                        "kind": "log_truncated",
                        "message": "activity log truncated at 5MB",
                        "detail": {},
                    }
                )
                activity_truncated = True
            else:
                activity_buffer.append(entry)
                activity_bytes += entry_size
        # Publish SSE regardless of buffer cap — live updates stay current.
        await publish(ReviewJobActivity(pr_id=row.pr_id, review_job_id=job_id, event=entry))

    try:
        ticket = await tickets.get(ticket_id, org_id=org_id)
        if ticket.pr_id is None:
            await _transition_failed(
                job_id, "ticket has no linked PR", org_id=org_id, activity_log=activity_buffer
            )
            return
        pr = await pull_requests.get(ticket.pr_id, org_id=org_id)
        vcs_plugin = get_vcs_plugin(pr.plugin_id)

        await _set_step(job_id, "fetching_diff", pr_id=pr.id)
        lessons = await memory.list_for_repo(pr.repo_external_id, org_id=org_id, plugin_id=pr.plugin_id)
        diff = await vcs_plugin.fetch_diff(pr.external_id)
        prior_comments = await vcs_plugin.list_yaaos_comments(pr.external_id)
        prior_bodies = [c.body for c in prior_comments]
        vcs_pr = await vcs_plugin.fetch_pr(pr.external_id)

        # Ticket-level skip checks.
        skip_reason = _ticket_skip_reason(pr, diff)
        if skip_reason is not None:
            await _transition_skipped(job_id, skip_reason, org_id=org_id, activity_log=activity_buffer)
            return

        # Secrets pre-flight.
        secret_rule = _detect_secrets(diff)
        if secret_rule is not None:
            try:
                await vcs_plugin.post_review(pr.external_id, _secrets_warning_review(secret_rule))
            except Exception:
                log.exception("review_job.secrets_warning_post_failed", review_job_id=str(job_id))
            await _transition_skipped(job_id, "secrets_detected", org_id=org_id, activity_log=activity_buffer)
            return

        language = _detect_language(diff)
        ctx = _ResolvedContext(
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

        await _set_step(job_id, "provisioning_workspace", pr_id=pr.id)
        async with with_workspace(
            "in_process",
            WorkspaceSpec(
                repo=RepoRefForSpec(plugin_id=pr.plugin_id, external_id=pr.repo_external_id),
                sha=pr.head_sha,
                branch_name=pr.head_branch,
                base_sha=ctx.vcs_pr.base_sha,
                base_branch=ctx.vcs_pr.base_branch,
                resource_caps=ResourceCaps(),
                network_policy=NetworkPolicy.GITHUB_ONLY,
                org_id=org_id,
            ),
            org_id=org_id,
        ) as ws:
            review_ctx = ReviewContext(
                pr=ctx.vcs_pr,
                diff=ctx.diff,
                lessons=ctx.lessons,
                language_hint=ctx.language,
                prior_yaaos_comment_bodies=ctx.prior_bodies,
                agent_config={},
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
                    prompt_hash=prompt_hash,
                    lessons_count=len(ctx.lessons),
                    lessons_applied=lesson_ids,
                    checkout_sha=ctx.vcs_pr.head_sha,
                    language_hint=ctx.language,
                ),
                actor=Actor.system(),
                org_id=org_id,
            )

            await _set_step(job_id, "invoking_agent", pr_id=pr.id)
            result = await coding_agent.review(
                plugin_id=_CODING_AGENT_PLUGIN_ID,
                workspace=ws,
                context=review_ctx,
                on_activity=_on_activity,
            )

        if result.status != InvocationStatus.SUCCESS:
            await _transition_failed(
                job_id,
                result.error_message or f"agent returned status={result.status}",
                org_id=org_id,
                invocation_status=str(result.status),
                raw_output_excerpt=(result.telemetry.raw_output or "")[:1000],
                activity_log=activity_buffer,
            )
            return

        # Post one Review to the VCS, then update the row + audit.
        await _set_step(job_id, "posting_review", pr_id=pr.id)
        review_obj = Review(
            agent_tag=_REVIEWER_TAG,
            state=result.state or "COMMENT",
            summary_body=result.summary_body,
            findings=result.findings,
        )
        post_result = await vcs_plugin.post_review(ctx.pr_external_id, review_obj)
        async with db_session() as s:
            for cid in post_result.finding_to_comment_external_id.values():
                s.add(
                    PostedCommentRow(
                        external_comment_id=cid,
                        org_id=org_id,
                        pr_id=ctx.pr_id,
                        review_job_id=job_id,
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
                    destination="vcs",
                    completed_at=_utcnow(),
                    review_external_id=post_result.review_external_id,
                    tokens_in=result.telemetry.tokens_in,
                    tokens_out=result.telemetry.tokens_out,
                    duration_s=duration,
                    current_step="posted",
                    findings=[f.model_dump(mode="json") for f in result.findings],
                    activity_log=activity_buffer,
                    # CLI may report a resolved model name (e.g. an `opus`
                    # alias becomes a versioned full name); persist that.
                    model=result.telemetry.model or _DEFAULT_MODEL,
                )
            )
            await s.commit()

        by_agent: dict[str, int] = {}
        for f in result.findings:
            key = f.source_agent or "unknown"
            by_agent[key] = by_agent.get(key, 0) + 1
        await audit_for_review_job(
            job_id,
            "review_job.posted",
            _PostedPayload(
                verdict=result.state or "COMMENT",
                finding_count=len(result.findings),
                findings_by_agent=by_agent,
                tokens_in=result.telemetry.tokens_in,
                tokens_out=result.telemetry.tokens_out,
                latency_ms=result.telemetry.latency_ms,
                review_external_id=post_result.review_external_id,
            ),
            actor=Actor.system(),
            org_id=org_id,
        )
        await publish(ReviewJobStatusChanged(pr_id=ctx.pr_id, review_job_id=job_id, status="posted"))

    except asyncio.CancelledError:
        # Operator-initiated cancel: the DB row was flipped to `cancelled`
        # and the `review_job.cancelled` audit was written by `cancel_pending`
        # BEFORE the task was cancelled. The workspace's `async with` exit
        # has already destroyed the tempdir and the CLI subprocess was killed
        # by `workspace.run_coding_agent_cli`'s CancelledError handler. Attach
        # whatever activity we captured before re-raising; cancel_pending
        # already wrote the status + audit.
        log.info("review_job.cancelled_mid_flight", review_job_id=str(job_id))
        if activity_buffer:
            try:
                async with db_session() as s:
                    await s.execute(
                        update(ReviewJobRow)
                        .where(ReviewJobRow.id == job_id)
                        .values(activity_log=activity_buffer)
                    )
                    await s.commit()
            except Exception:
                log.exception("review_job.cancel_persist_failed", review_job_id=str(job_id))
        raise

    except Exception as e:
        log.exception("review_job.handler_crashed", review_job_id=str(job_id))
        await _transition_failed(
            job_id,
            f"handler crashed: {e}",
            org_id=org_id,
            invocation_status="crashed",
            activity_log=activity_buffer,
        )


def _ticket_skip_reason(pr: Any, diff: Diff) -> str | None:
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


async def _transition_failed(
    job_id: UUID,
    error: str,
    *,
    org_id: UUID,
    invocation_status: str = "agent_error",
    raw_output_excerpt: str = "",
    activity_log: list[dict[str, Any]] | None = None,
) -> None:
    values: dict[str, Any] = {
        "status": "failed",
        "completed_at": _utcnow(),
        "error_message": error,
        "current_step": "failed",
    }
    if activity_log is not None:
        values["activity_log"] = activity_log
    async with db_session() as s:
        await s.execute(update(ReviewJobRow).where(ReviewJobRow.id == job_id).values(**values))
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


async def _transition_skipped(
    job_id: UUID,
    reason: str,
    *,
    org_id: UUID,
    activity_log: list[dict[str, Any]] | None = None,
) -> None:
    values: dict[str, Any] = {
        "status": "skipped",
        "skip_reason": reason,
        "completed_at": _utcnow(),
    }
    if activity_log is not None:
        values["activity_log"] = activity_log
    async with db_session() as s:
        await s.execute(update(ReviewJobRow).where(ReviewJobRow.id == job_id).values(**values))
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


_SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("private_key_pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


def _detect_secrets(diff: Diff) -> str | None:
    for raw_line in (diff.raw or "").splitlines():
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        for rule_id, pat in _SECRET_RULES:
            if pat.search(raw_line):
                return rule_id
    return None


def _secrets_warning_review(rule_id: str) -> Review:
    body = (
        "yaaos refused to review this PR — the diff contains content that "
        f"looks like a leaked secret (rule: `{rule_id}`). Remove the secret, "
        "rotate it on the upstream provider, then push a fresh commit and the "
        "review will run automatically."
    )
    return Review(agent_tag=_REVIEWER_TAG, state="COMMENT", summary_body=body, findings=[])


async def _set_step(job_id: UUID, step: str, *, pr_id: UUID) -> None:
    async with db_session() as s:
        await s.execute(
            update(ReviewJobRow)
            .where(ReviewJobRow.id == job_id)
            .values(current_step=step, last_heartbeat_at=_utcnow())
        )
        await s.commit()
    await publish(ReviewJobStepProgress(pr_id=pr_id, review_job_id=job_id, current_step=step))


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


async def startup_recovery() -> None:
    """Mark any `running` jobs from a prior process as failed; respawn `queued` jobs."""
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

    # Resolve ticket_id per queued job (via the PR row) so we can respawn.
    from app.domain.pull_requests.models import PullRequestRow  # noqa: PLC0415

    for row in queued:
        async with db_session() as s:
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
                    org_id=row.org_id,
                    debounce_seconds=0,
                )
            ),
        )
