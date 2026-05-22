"""Legacy `review_jobs` runner — `_run_review_job_inner` + glue.

Extracted from `queue.py` so the file responsible for the legacy code
path is named for what it does. The runner provisions a workspace,
invokes the coding-agent CLI, posts the Review back to the VCS, and
writes audit rows at every transition.

This file shrinks to empty when the legacy `review_jobs` table is
dropped (M05 close-out). Until then the public scheduling API
(`schedule_review`, `cancel_pending`, `startup_recovery` in `queue.py`)
spawns these coroutines via `core/observability.spawn`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, update

from app.core.audit_log import Actor, audit_for_review_job
from app.core.database import session as db_session
from app.core.events import publish
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
from app.domain.coding_agent import ActivityEvent, InvocationStatus, ReviewContext
from app.domain.reviewer.admission import (
    findingdrafts_to_raw as _findingdrafts_to_raw,
)
from app.domain.reviewer.admission import (
    raw_to_vcs_findings as _raw_to_vcs_findings,
)
from app.domain.reviewer.constants import (
    CODING_AGENT_PLUGIN_ID as _CODING_AGENT_PLUGIN_ID,
)
from app.domain.reviewer.constants import (
    DEFAULT_MODEL as _DEFAULT_MODEL,
)
from app.domain.reviewer.constants import (
    REVIEWER_TAG as _REVIEWER_TAG,
)
from app.domain.reviewer.diff_utils import (
    detect_language as _detect_language,
)
from app.domain.reviewer.diff_utils import (
    ticket_skip_reason as _ticket_skip_reason,
)
from app.domain.reviewer.lock import acquire_pr_lock
from app.domain.reviewer.mcp_wiring import build_mcp_payload as _build_mcp_payload
from app.domain.reviewer.mcp_wiring import (
    prefix_broken_creds_warning as _prefix_broken_creds_warning,
)
from app.domain.reviewer.models import ReviewRow
from app.domain.reviewer.queue_events import (
    ReviewJobActivity,
    ReviewJobStatusChanged,
)
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.review_job import ReviewJobInput
from app.domain.reviewer.review_job_transitions import (
    AdmissionDropsPayload as _AdmissionDropsPayload,
)
from app.domain.reviewer.review_job_transitions import (
    PostedPayload as _PostedPayload,
)
from app.domain.reviewer.review_job_transitions import (
    PromptSentPayload as _PromptSentPayload,
)
from app.domain.reviewer.review_job_transitions import (
    set_step as _set_step,
)
from app.domain.reviewer.review_job_transitions import (
    transition_failed as _transition_failed,
)
from app.domain.reviewer.review_job_transitions import (
    transition_skipped as _transition_skipped,
)
from app.domain.reviewer.secrets_detection import detect_secrets as _detect_secrets
from app.domain.reviewer.secrets_detection import (
    secrets_warning_review as _secrets_warning_review,
)
from app.domain.reviewer.service import dispatch_audits, dispatch_events
from app.domain.vcs import Diff, Review, VCSPullRequest
from app.domain.vcs import (
    get_plugin as get_vcs_plugin,
)

log = structlog.get_logger("reviewer.legacy_runner")


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
                        session=s,
                    )
                    await s.commit()

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
                        async with db_session() as s:
                            await mcp_proxy.revoke_token(job_id, session=s)
                            await s.commit()
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
                    session=s,
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
        async with db_session() as s:
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
                session=s,
            )
            await s.commit()
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
