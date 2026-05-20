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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlalchemy import select, update

from app.core.audit_log import Actor, audit_for_review_job
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.events import Event, publish
from app.core.observability import spawn
from app.core.workspace import (
    NetworkPolicy,
    RepoRefForSpec,
    ResourceCaps,
    WorkspaceSpec,
    with_workspace,
)
from app.domain import (
    coding_agent,
    mcp_proxy,
    memory,
    pull_requests,
    tickets,
)
from app.domain import (
    integrations as mcp_integrations,
)
from app.domain.coding_agent import ActivityEvent, FindingDraft, InvocationStatus, ReviewContext
from app.domain.reviewer.aggregate import RawFinding
from app.domain.reviewer.anchor import make_anchor
from app.domain.reviewer.fingerprint import compute_fingerprint
from app.domain.reviewer.lock import acquire_pr_lock
from app.domain.reviewer.models import ReviewRow
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.service import dispatch_audits, dispatch_events
from app.domain.reviewer.types import (
    Severity,
)
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


class _AdmissionDropsPayload(BaseModel):
    """Audit payload for plan §10.5 admission drops (one row per review)."""

    drops: list[dict[str, Any]]


def _prefix_broken_creds_warning(body: str | None, providers: list[str]) -> str | None:
    """Prefix the PR review summary with a yellow GitHub callout listing any
    MCP providers that returned `broken_creds`/`not_connected` during this
    review. No-op when nothing was observed."""
    if not providers:
        return body
    names = ", ".join(providers)
    note = (
        "> [!WARNING]\n"
        f"> The following MCP integrations returned errors during this review "
        f"and were skipped: **{names}**. Reconnect them in Org Settings → Integrations.\n"
    )
    if not body:
        return note
    return f"{note}\n{body}"


async def _build_mcp_payload(review_id: UUID, *, org_id: UUID) -> dict[str, Any] | None:
    """Collect connected MCP providers for the org and mint a per-review bearer.

    Returns None when no providers are connected (or all are broken/disabled) —
    the reviewer still runs, just without MCP context. The bearer + provider
    catalogue are threaded into the agent via `ReviewContext.agent_config["mcp"]`;
    `plugins/claude_code` materializes the workspace `.mcp.json` from it.
    """
    servers: list[dict[str, Any]] = []
    async with db_session() as s:
        for provider_id in mcp_integrations.known_providers():
            row = await mcp_integrations.get(s, org_id, provider_id)
            if row is None or not row.enabled:
                continue
            if row.last_refresh_status == "failed":
                log.warning(
                    "review.mcp.broken_creds_skipped",
                    provider=provider_id,
                    org_id=str(org_id),
                )
                continue
            prov = mcp_integrations.get_provider(provider_id)
            servers.append(
                {
                    "provider": provider_id,
                    "allowed_tools": list(row.allowed_tools),
                    "known_read_tools": list(prov.config.known_read_tools) if prov else [],
                    "known_write_tools": list(prov.config.known_write_tools) if prov else [],
                }
            )
    if not servers:
        log.info("review.mcp.no_connected_providers", org_id=str(org_id))
        return None
    raw_token = await mcp_proxy.mint_token(review_id)
    return {
        "token": raw_token,
        "base_url": f"{get_settings().yaaos_app_base_url}/api/mcp/{review_id}",
        "servers": servers,
    }


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

    new_id = uuid4()
    # Acquire the per-PR advisory lock for the whole cancel + insert flow so
    # the sequence_number computation can't race two concurrent schedule_review
    # calls into the UNIQUE(pr_id, sequence_number) constraint.
    async with db_session() as s:
        await acquire_pr_lock(s, pr.id)
        # Cancel any in-flight review for this PR.
        inflight = (
            (
                await s.execute(
                    select(ReviewRow).where(
                        ReviewRow.pr_id == pr.id,
                        ReviewRow.status.in_(["queued", "running"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in inflight:
            await s.execute(
                update(ReviewRow)
                .where(ReviewRow.id == row.id)
                .values(
                    status="cancelled",
                    skip_reason="superseded",
                    completed_at=_utcnow(),
                    # Plan §6.3 / §7: the cancelled row points at the new
                    # review that superseded it so the UI / audit can chain
                    # them.
                    superseded_by_review_id=new_id,
                )
            )
        max_seq = (
            await s.execute(
                select(sa_func.coalesce(sa_func.max(ReviewRow.sequence_number), 0)).where(
                    ReviewRow.pr_id == pr.id
                )
            )
        ).scalar_one()
        s.add(
            ReviewRow(
                id=new_id,
                org_id=org_id,
                pr_id=pr.id,
                sequence_number=max_seq + 1,
                status="queued",
                trigger_reason=trigger_reason,
                destination="vcs",
                # Generation-2 scope: schedule_review is always a "full" run.
                # Incremental scope is owned by handle_push (§6.2).
                scope_kind="full",
                model=_DEFAULT_MODEL,
                effort=_DEFAULT_EFFORT,
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
        _run_review_job_with_context(
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
                    select(ReviewRow).where(
                        ReviewRow.pr_id == ticket.pr_id,
                        ReviewRow.status.in_(["queued", "running"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in inflight:
            await s.execute(
                update(ReviewRow)
                .where(ReviewRow.id == row.id)
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
    trigger_reason: str
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
    def from_row(cls, row: ReviewRow) -> ReviewJob:
        return cls(
            id=row.id,
            org_id=row.org_id,
            pr_id=row.pr_id,
            status=row.status,
            trigger_reason=row.trigger_reason,
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
                select(ReviewRow).where(ReviewRow.id == review_job_id, ReviewRow.org_id == org_id)
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
                    select(ReviewRow)
                    .where(ReviewRow.pr_id == pr_id, ReviewRow.org_id == org_id)
                    .order_by(ReviewRow.created_at.desc())
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
                    select(ReviewRow).where(
                        ReviewRow.org_id == org_id,
                        ReviewRow.status.in_(["queued", "running"]),
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
        rows = (await s.execute(select(ReviewRow).where(ReviewRow.org_id == org_id))).scalars().all()
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


async def _run_review_job_with_context(input: ReviewJobInput) -> None:
    """Phase 9: wrap `_run_review_job_inner` in `org_context(...)` so
    background audit rows + structlog lines carry the correct org +
    workspace actor."""
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    async with org_context(input.org_id, ActorKind.WORKSPACE):
        await _run_review_job_inner(input)


async def _run_review_job_inner(input: ReviewJobInput) -> None:
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
        row = (await s.execute(select(ReviewRow).where(ReviewRow.id == job_id))).scalar_one_or_none()
    if row is None or row.status != "queued":
        return

    async with db_session() as s:
        await s.execute(
            update(ReviewRow)
            .where(ReviewRow.id == job_id)
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

        # MCP context (Phase 3): per-review bearer + per-org connected providers.
        # Token minted before the workspace so we can fail fast on DB errors;
        # revoked in `finally` below, BEFORE workspace teardown.
        mcp_payload = await _build_mcp_payload(job_id, org_id=org_id)

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
            try:
                review_ctx = ReviewContext(
                    pr=ctx.vcs_pr,
                    diff=ctx.diff,
                    lessons=ctx.lessons,
                    language_hint=ctx.language,
                    prior_yaaos_comment_bodies=ctx.prior_bodies,
                    agent_config={"mcp": mcp_payload} if mcp_payload is not None else {},
                )
                prompt_hash = hashlib.sha256(review_ctx.model_dump_json().encode()).hexdigest()
                lesson_ids = [lesson.id for lesson in ctx.lessons]

                async with db_session() as s:
                    await s.execute(
                        update(ReviewRow)
                        .where(ReviewRow.id == job_id)
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

                # Plan §2.3: pre-load file contents for each draft's anchor while
                # the workspace is still mounted; anchor + fingerprint hashes need
                # real file content (NOT the body text).
                draft_file_contents: dict[str, list[str] | None] = {}
                for draft in result.findings or []:
                    fp = draft.anchor.file_path
                    if fp in draft_file_contents:
                        continue
                    text = await ws.read_text(fp)
                    draft_file_contents[fp] = None if text is None else text.splitlines()
            finally:
                # Revoke BEFORE the workspace context exits (and tears down the
                # tempdir). Idempotent — sweep handles anything we miss.
                if mcp_payload is not None:
                    try:
                        await mcp_proxy.revoke_token(job_id)
                    except Exception:
                        log.exception("review_job.mcp_revoke_failed", review_job_id=str(job_id))

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

        # Plan §6.1 + §13 cutover: admission BEFORE posting. The previous
        # legacy flow posted everything the agent emitted and gated the
        # durable-findings persist behind admission — rejected findings still
        # ended up on GitHub. Now admit first via the aggregate, then post
        # only the survivors.
        await _set_step(job_id, "posting_review", pr_id=pr.id)
        async with db_session() as s:
            await acquire_pr_lock(s, ctx.pr_id)
            agg_repo = SqlAlchemyAggregateRepository(s)
            aggregate = await agg_repo.load(pr_id=ctx.pr_id, org_id=org_id)

            raw = _findingdrafts_to_raw(
                result.findings,
                commit_sha=ctx.vcs_pr.head_sha,
                read_file=draft_file_contents.get,
            )
            # Plan §10.9: drop findings whose anchor file isn't in the PR diff.
            diff_files = {f.path for f in (ctx.diff.files or [])}
            new_findings, _obs, drops = aggregate.post_process_raw_findings(
                job_id, raw, diff_files=diff_files
            )

            # Translate admitted RawFindings back to vcs.Finding for posting.
            posted_vcs_findings = _raw_to_vcs_findings(raw, new_findings)
            broken_providers = sorted(mcp_proxy.consume_broken_creds(job_id))
            summary_body = _prefix_broken_creds_warning(result.summary_body, broken_providers)
            review_obj = Review(
                agent_tag=_REVIEWER_TAG,
                state=result.state or "COMMENT",
                summary_body=summary_body,
                findings=posted_vcs_findings,
            )
            post_result = await vcs_plugin.post_review(ctx.pr_external_id, review_obj)

            # Build threads + posted yaaos messages so the §9.3 All Conversations
            # view + §9.4 thread rendering have content. Map back from
            # `post_result.finding_to_comment_external_id` (keyed by vcs.Finding
            # index) to the matching durable Finding.
            external_ids = list(post_result.finding_to_comment_external_id.values())
            for idx, f in enumerate(new_findings):
                external_id = external_ids[idx] if idx < len(external_ids) else f"local-{f.id}"
                thread = aggregate.open_thread_for_finding(f.id)
                aggregate.append_message(
                    thread_id=thread.id,
                    author_kind="yaaos",
                    author_external_id=_REVIEWER_TAG,
                    external_comment_id=external_id,
                    body=f.body,
                )
            aggregate.complete_review(job_id, [f.id for f in new_findings])
            await agg_repo.save(aggregate)
            await dispatch_audits(aggregate, session=s, actor=Actor.system(), org_id=org_id)
            await dispatch_events(aggregate)
            if drops:
                drops_payload = [
                    {
                        "rule_id": d.rule_id,
                        "reason": d.reason,
                        "severity": d.severity,
                        "confidence": d.confidence,
                    }
                    for d in drops
                ]
                log.info("review_job.admission_drops", review_job_id=str(job_id), drops=drops_payload)
                await audit_for_review_job(
                    job_id,
                    "review_job.findings_dropped",
                    _AdmissionDropsPayload(drops=drops_payload),
                    actor=Actor.system(),
                    org_id=org_id,
                )
            started = (await s.execute(select(ReviewRow).where(ReviewRow.id == job_id))).scalar_one_or_none()
            duration = None
            if started and started.started_at:
                duration = int((_utcnow() - started.started_at).total_seconds())
            await s.execute(
                update(ReviewRow)
                .where(ReviewRow.id == job_id)
                .values(
                    status="posted",
                    destination="vcs",
                    completed_at=_utcnow(),
                    review_external_id=post_result.review_external_id,
                    tokens_in=result.telemetry.tokens_in,
                    tokens_out=result.telemetry.tokens_out,
                    duration_s=duration,
                    current_step="posted",
                    commit_sha_at_start=ctx.vcs_pr.head_sha,
                    # Plan §13: agent emits FindingDraft (§10.1); cache the
                    # ADMITTED subset (rejected drafts never reach GitHub).
                    findings=[
                        {
                            "file_path": r.anchor.file_path,
                            "line_start": r.anchor.line_start,
                            "line_end": r.anchor.line_end,
                            "severity": r.severity,
                            "rule_id": r.rule_id,
                            "title": r.title,
                            "body": r.body,
                            "rationale": r.rationale,
                            "source_agent": r.source_agent,
                        }
                        for r in raw
                        if r.fingerprint.hash in {f.fingerprint.hash for f in new_findings}
                    ],
                    activity_log=activity_buffer,
                    # CLI may report a resolved model name (e.g. an `opus`
                    # alias becomes a versioned full name); persist that.
                    model=result.telemetry.model or _DEFAULT_MODEL,
                )
            )
            await s.commit()

        # FindingDrafts don't carry `source_agent` directly — it's set during
        # _findingdrafts_to_raw. Group admitted findings by their assigned tag.
        by_agent: dict[str, int] = {}
        for r in raw:
            if r.fingerprint.hash not in {f.fingerprint.hash for f in new_findings}:
                continue
            key = r.source_agent or "unknown"
            by_agent[key] = by_agent.get(key, 0) + 1
        await audit_for_review_job(
            job_id,
            "review_job.posted",
            _PostedPayload(
                verdict=result.state or "COMMENT",
                finding_count=len(new_findings),
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

        # Plan §10.13 — POC eval metrics. Log tier mix (severity distribution)
        # so an operator can see whether the reviewer is generating Tier 1/2
        # signal vs noise. The acceptance-rate / resolved-without-edit metrics
        # need the durable-findings reply lifecycle to mature; logged here
        # so the data starts accumulating now.
        severity_mix: dict[str, int] = {}
        for f in new_findings:
            severity_mix[f.severity] = severity_mix.get(f.severity, 0) + 1
        # Plan §10.13 tier 1+2 = blocker + major in §10.1 vocab.
        tier_1_2 = severity_mix.get("blocker", 0) + severity_mix.get("major", 0)
        total = sum(severity_mix.values())
        log.info(
            "review_job.eval_metrics",
            review_job_id=str(job_id),
            pr_id=str(ctx.pr_id),
            posted_count=total,
            severity_mix=severity_mix,
            tier_1_2_ratio=(tier_1_2 / total) if total else 0.0,
        )

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
                        update(ReviewRow).where(ReviewRow.id == job_id).values(activity_log=activity_buffer)
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
        await s.execute(update(ReviewRow).where(ReviewRow.id == job_id).values(**values))
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
        await s.execute(update(ReviewRow).where(ReviewRow.id == job_id).values(**values))
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
            update(ReviewRow)
            .where(ReviewRow.id == job_id)
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


_SEVERITY_TO_VCS: dict[Severity, str] = {
    "blocker": "must-fix",
    "major": "must-fix",
    "minor": "suggestion",
    "nit": "nit",
}


def _findingdrafts_to_raw(
    drafts: list[FindingDraft],
    *,
    commit_sha: str,
    read_file: Callable[[str], list[str] | None],
    source_agent: str = "coding_agent",
) -> list[RawFinding]:
    """Convert §10.1 `FindingDraft`s from the agent into `RawFinding`s.

    Plan §2.3: anchor + fingerprint hashes use real file content at the
    anchored line range — never the body text. Two findings at the same
    file:line with different body phrasings must produce IDENTICAL
    fingerprints so the aggregate deduplicates re-observations across
    reviews. Drafts whose file we can't read are dropped — no stable
    fingerprint without real content.

    Shared between full review (queue.py) and incremental review
    (incremental.py); both go through the same admission pipeline.
    """
    out: list[RawFinding] = []
    for d in drafts:
        file_lines = read_file(d.anchor.file_path)
        # `None` = file missing; `[]` = file present but empty. Both fail the
        # same way — no stable anchor / fingerprint without real content.
        if not file_lines:
            log.info(
                "review.findingdraft_dropped_no_file",
                file=d.anchor.file_path,
                rule_id=d.rule_id,
            )
            continue
        # Defensive clamp — plan §10.1 enforces a valid range on the agent
        # but we don't want make_anchor to raise on off-by-one drafts.
        ls = max(1, min(d.anchor.line_start, len(file_lines)))
        le = max(ls, min(d.anchor.line_end, len(file_lines)))
        anchor = make_anchor(
            file_path=d.anchor.file_path,
            file_lines=file_lines,
            line_start=ls,
            line_end=le,
            commit_sha=commit_sha,
        )
        anchored_lines = file_lines[ls - 1 : le]
        fingerprint = compute_fingerprint(
            file_path=d.anchor.file_path,
            rule_id=d.rule_id,
            anchored_lines=anchored_lines,
            title=d.title,
        )
        out.append(
            RawFinding(
                fingerprint=fingerprint,
                rule_id=d.rule_id,
                title=d.title,
                body=d.body,
                rationale=d.rationale,
                concrete_failure_scenario=d.concrete_failure_scenario,
                confidence=d.confidence,
                severity=d.severity,
                anchor=anchor,
                source_agent=source_agent,
                duplicate_of_rule_ids=d.duplicate_of_rule_ids,
            )
        )
    return out


def _raw_to_vcs_findings(raw: list[RawFinding], new_findings: list[Any]) -> list[Any]:
    """Map admitted RawFinding back into vcs.Finding payloads for posting.

    Only admitted findings (post-aggregate-gate) translate; rejected ones
    never reach the VCS plugin. Severity collapses plan §10.1's four tiers
    onto the legacy VCS three-tier enum.
    """
    from app.domain.vcs import Finding as VcsFinding  # noqa: PLC0415

    out: list[Any] = []
    admitted_fps = {f.fingerprint.hash for f in new_findings}
    for r in raw:
        if r.fingerprint.hash not in admitted_fps:
            continue
        out.append(
            VcsFinding(
                file=r.anchor.file_path,
                line_start=r.anchor.line_start,
                line_end=r.anchor.line_end,
                severity=_SEVERITY_TO_VCS.get(r.severity, "suggestion"),
                title=r.title,
                body=r.body,
                rationale=r.rationale,
                snippet=None,
                applied_lesson_ids=[],
                source_agent=r.source_agent,
            )
        )
    return out


async def startup_recovery() -> None:
    """Mark any `running` jobs from a prior process as failed; respawn `queued` jobs."""
    async with db_session() as s:
        crashed = (await s.execute(select(ReviewRow.id).where(ReviewRow.status == "running"))).scalars().all()
        if crashed:
            await s.execute(
                update(ReviewRow)
                .where(ReviewRow.status == "running")
                .values(
                    status="failed",
                    skip_reason="crashed",
                    completed_at=_utcnow(),
                    error_message="process crashed mid-execution",
                )
            )
        queued = (await s.execute(select(ReviewRow).where(ReviewRow.status == "queued"))).scalars().all()
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
            _run_review_job_with_context(
                ReviewJobInput(
                    review_job_id=row.id,
                    ticket_id=pr_row.ticket_id,
                    org_id=row.org_id,
                    debounce_seconds=0,
                )
            ),
        )
