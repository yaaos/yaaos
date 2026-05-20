"""Auto-incremental review on push (plan §6.2).

`handle_push` is the entry point — called from intake when a `pull_request
synchronize` webhook fires (or wherever else a push arrives). It runs the
§7 trigger policy and either skips, debounces, or spawns an incremental-
review runner. The runner uses `coding_agent.incremental_review` (which
operates on the `prev_sha..head_sha` slice) and persists through the
aggregate — durable findings, threads, and messages — rather than legacy
JSONB.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import desc, select, update

from app.core.audit_log import Actor
from app.core.database import session as db_session
from app.core.events import publish
from app.core.observability import spawn
from app.core.workspace import (
    NetworkPolicy,
    RepoRefForSpec,
    ResourceCaps,
    WorkspaceSpec,
    with_workspace,
)
from app.domain import coding_agent, memory, pull_requests, tickets
from app.domain.coding_agent import (
    IncrementalReviewContext,
    InvocationStatus,
    StaleCheckContext,
)
from app.domain.reviewer.aggregate import PRReviewAggregate as _Aggregate
from app.domain.reviewer.anchor import resolve_anchor
from app.domain.reviewer.lock import acquire_pr_lock
from app.domain.reviewer.models import ReviewRow
from app.domain.reviewer.queue import (
    _CODING_AGENT_PLUGIN_ID,
    _DEFAULT_EFFORT,
    _DEFAULT_MODEL,
    _REVIEWER_TAG,
    ReviewJobActivity,
    ReviewJobStatusChanged,
    _detect_language,
    _findingdrafts_to_raw,
    _raw_to_vcs_findings,
    _set_step,
    _ticket_skip_reason,
    _utcnow,
)
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.service import apply_stale_check_result, dispatch_audits, dispatch_events
from app.domain.reviewer.trigger import (
    Debounce,
    Run,
    Skip,
    TriggerInputs,
    decide_trigger,
    humanize_skip,
)
from app.domain.reviewer.types import FindingState
from app.domain.vcs import Review
from app.domain.vcs import get_plugin as get_vcs_plugin

log = structlog.get_logger("reviewer.incremental")


_DEBOUNCE_WINDOW_SECONDS = 30


async def handle_push(
    pr_id: UUID,
    *,
    new_head_sha: str,
    prev_head_sha: str | None,
    org_id: UUID,
) -> str | None:
    """Decide whether the push warrants an incremental review and spawn one.

    Returns a short status string for the caller's audit / log line:
    `"scheduled"`, `"skipped:<reason>"`, or `"debounced:<seconds>"`.

    `prev_head_sha` comes from the webhook payload (`synchronize.before`).
    The trigger policy's `last_reviewed_sha` separately tracks the last
    posted-review's commit; we prefer that as the incremental scope start
    (so a series of pushes between reviews coalesce into one diff). If no
    prior review exists, we fall back to `prev_head_sha`.
    """
    pr = await pull_requests.get(pr_id, org_id=org_id)
    last_reviewed_sha = await _last_reviewed_sha(pr_id)
    in_flight_id = await _in_flight_review_id(pr_id)
    last_push_at = await _last_push_timestamp(pr_id)

    # Effective prev for the scope: prefer last-reviewed SHA; fall back to
    # webhook's `before`. Either way the trigger policy needs both to make
    # the ancestor + base-merge checks.
    effective_prev = last_reviewed_sha or prev_head_sha
    ancestor_ok = await _is_ancestor(pr.plugin_id, pr.repo_external_id, effective_prev, new_head_sha)
    new_commit_messages = await _new_commit_messages(
        pr.plugin_id, pr.repo_external_id, effective_prev, new_head_sha
    )

    inputs = TriggerInputs(
        pr_is_draft=pr.is_draft,
        last_reviewed_sha=effective_prev,
        head_sha=new_head_sha,
        in_flight_review_id=str(in_flight_id) if in_flight_id else None,
        new_commit_messages=new_commit_messages,
        last_reviewed_sha_is_ancestor=ancestor_ok,
        last_push_at=last_push_at,
        now=datetime.now(UTC),
        debounce_window_seconds=_DEBOUNCE_WINDOW_SECONDS,
    )

    decision = decide_trigger(inputs)
    if isinstance(decision, Skip):
        log.info(
            "incremental.skipped",
            pr_id=str(pr_id),
            reason=decision.reason,
            human=humanize_skip(decision.reason),
        )
        # If something is in-flight, flip pending_replay so the trigger policy
        # re-evaluates when the current review completes (plan §7 rule 4).
        if decision.reason == "in_flight" and in_flight_id is not None:
            async with db_session() as s:
                await s.execute(
                    update(ReviewRow).where(ReviewRow.id == in_flight_id).values(pending_replay=True)
                )
                await s.commit()
        return f"skipped:{decision.reason}"

    if isinstance(decision, Debounce):
        log.info(
            "incremental.debounced",
            pr_id=str(pr_id),
            seconds_remaining=decision.seconds_remaining,
        )
        # Spawn a delayed re-check so the debounce window self-resolves.
        spawn(
            f"incremental_debounce:{pr_id}:{new_head_sha[:8]}",
            _debounce_then_retry(
                pr_id=pr_id,
                new_head_sha=new_head_sha,
                prev_head_sha=prev_head_sha,
                org_id=org_id,
                delay=decision.seconds_remaining,
            ),
        )
        return f"debounced:{int(decision.seconds_remaining)}"

    assert isinstance(decision, Run)
    review_id = await _create_incremental_review(
        pr_id=pr_id,
        org_id=org_id,
        prev_sha=decision.scope.base_sha,
        head_sha=decision.scope.head_sha,
    )
    ticket = await tickets.get_by_pr(pr_id, org_id=org_id)
    if ticket is None:
        log.warning("incremental.no_ticket", pr_id=str(pr_id))
        return "skipped:no_ticket"
    spawn(
        f"incremental_review:{review_id}",
        _run_incremental_review(
            review_id=review_id,
            ticket_id=ticket.id,
            org_id=org_id,
            prev_sha=decision.scope.base_sha,
            head_sha=decision.scope.head_sha,
        ),
    )
    return "scheduled"


async def _debounce_then_retry(
    *, pr_id: UUID, new_head_sha: str, prev_head_sha: str | None, org_id: UUID, delay: float
) -> None:
    await asyncio.sleep(max(0.0, delay))
    await handle_push(pr_id, new_head_sha=new_head_sha, prev_head_sha=prev_head_sha, org_id=org_id)


async def _create_incremental_review(*, pr_id: UUID, org_id: UUID, prev_sha: str, head_sha: str) -> UUID:
    from sqlalchemy import func as sa_func  # noqa: PLC0415

    new_id = uuid4()
    async with db_session() as s:
        await acquire_pr_lock(s, pr_id)
        max_seq = (
            await s.execute(
                select(sa_func.coalesce(sa_func.max(ReviewRow.sequence_number), 0)).where(
                    ReviewRow.pr_id == pr_id
                )
            )
        ).scalar_one()
        s.add(
            ReviewRow(
                id=new_id,
                org_id=org_id,
                pr_id=pr_id,
                sequence_number=max_seq + 1,
                status="queued",
                trigger_reason="push_incremental",
                destination="vcs",
                scope_kind="incremental",
                scope_prev_sha=prev_sha,
                commit_sha_at_start=head_sha,
                model=_DEFAULT_MODEL,
                effort=_DEFAULT_EFFORT,
            )
        )
        await s.commit()
    await publish(ReviewJobStatusChanged(pr_id=pr_id, review_job_id=new_id, status="queued"))
    return new_id


async def _run_incremental_review(
    *,
    review_id: UUID,
    ticket_id: UUID,
    org_id: UUID,
    prev_sha: str,
    head_sha: str,
) -> None:
    """Workspace + coding_agent.incremental_review + aggregate persist + post."""
    try:
        ticket = await tickets.get(ticket_id, org_id=org_id)
        if ticket.pr_id is None:
            await _fail_review(review_id, "ticket has no PR")
            return
        pr = await pull_requests.get(ticket.pr_id, org_id=org_id)
        vcs_plugin = get_vcs_plugin(pr.plugin_id)
        vcs_pr = await vcs_plugin.fetch_pr(pr.external_id)
        diff = await vcs_plugin.fetch_diff(pr.external_id)
        lessons = await memory.list_for_repo(pr.repo_external_id, org_id=org_id, plugin_id=pr.plugin_id)
        language = _detect_language(diff)

        async with db_session() as s:
            await s.execute(
                update(ReviewRow)
                .where(ReviewRow.id == review_id)
                .values(status="running", started_at=_utcnow(), current_step="resolving_entities")
            )
            await s.commit()
        await publish(ReviewJobStatusChanged(pr_id=pr.id, review_job_id=review_id, status="running"))

        skip_reason = _ticket_skip_reason(pr, diff)
        if skip_reason is not None:
            await _skip_review(review_id, skip_reason)
            return

        # Load prior open + acknowledged findings so the agent can avoid
        # re-raising them (the aggregate will dedup on fingerprint, but the
        # prompt-level instruction reduces wasted work).
        async with db_session() as s:
            agg_repo = SqlAlchemyAggregateRepository(s)
            aggregate = await agg_repo.load(pr_id=pr.id, org_id=org_id)
        prior_open = [
            f"{f.title} ({f.current_anchor.file_path}:{f.current_anchor.line_start})"
            for f in aggregate.findings
            if f.state == FindingState.OPEN
        ]
        prior_ack = [
            f"{f.title} ({f.current_anchor.file_path}:{f.current_anchor.line_start})"
            for f in aggregate.findings
            if f.state == FindingState.ACKNOWLEDGED
        ]

        await _set_step(review_id, "provisioning_workspace", pr_id=pr.id)
        async with with_workspace(
            "in_process",
            WorkspaceSpec(
                repo=RepoRefForSpec(plugin_id=pr.plugin_id, external_id=pr.repo_external_id),
                sha=vcs_pr.head_sha,
                branch_name=vcs_pr.head_branch,
                base_sha=vcs_pr.base_sha,
                base_branch=vcs_pr.base_branch,
                resource_caps=ResourceCaps(),
                network_policy=NetworkPolicy.GITHUB_ONLY,
                org_id=org_id,
            ),
            org_id=org_id,
        ) as ws:
            ctx = IncrementalReviewContext(
                pr=vcs_pr,
                diff=diff,
                prev_sha=prev_sha,
                head_sha=head_sha,
                lessons=lessons,
                language_hint=language,
                prior_open_finding_summaries=prior_open,
                prior_acknowledged_finding_summaries=prior_ack,
                agent_config={},
            )
            await _set_step(review_id, "invoking_agent", pr_id=pr.id)

            async def _on_activity(event: Any) -> None:
                await publish(
                    ReviewJobActivity(
                        pr_id=pr.id, review_job_id=review_id, event=event.model_dump(mode="json")
                    )
                )

            result = await coding_agent.incremental_review(
                plugin_id=_CODING_AGENT_PLUGIN_ID,
                workspace=ws,
                context=ctx,
                on_activity=_on_activity,
            )

            # Plan §6.2 step 4b: re-check each currently-open finding whose
            # anchor file is in the touched diff. Deterministic anchor
            # re-resolution runs first (cheap + reliable for "did the block
            # move?"); the LLM stale_check then runs only on findings whose
            # anchor still resolves, to catch the "code still here but
            # changed semantics" case. Captured inside `with_workspace`
            # because the helper reads file contents from the workspace.
            touched_files = {f.path for f in diff.files} if diff.files else set()
            stale_results: list[tuple[UUID, Any]] = []  # (finding_id, StaleCheckResult)
            anchor_moved_finding_ids: set[UUID] = set()
            anchor_gone_finding_ids: set[UUID] = set()
            anchor_moved_snapshots: dict[UUID, Any] = {}  # finding_id → snapshot finding
            if touched_files:
                # Load a snapshot of open findings without holding a session.
                async with db_session() as snapshot_s:
                    snapshot_repo = SqlAlchemyAggregateRepository(snapshot_s)
                    snapshot_agg = await snapshot_repo.load(pr_id=pr.id, org_id=org_id)

                # Deterministic anchor pass (plan §6.2 step 4b). Pre-load
                # file contents async, then call the pure helper.
                files_needed = {
                    f.current_anchor.file_path for f in snapshot_agg.open_findings_in_files(touched_files)
                }
                contents: dict[str, list[str] | None] = {}
                for fp in files_needed:
                    text = await ws.read_text(fp)
                    contents[fp] = None if text is None else text.splitlines()
                anchor_result = resolve_open_anchors(
                    snapshot_agg,
                    touched_files=touched_files,
                    read_file=contents.get,
                    new_commit_sha=head_sha,
                )
                anchor_moved_finding_ids = set(anchor_result.moved)
                anchor_gone_finding_ids = set(anchor_result.gone)
                anchor_moved_snapshots = {
                    f.id: f for f in snapshot_agg.findings if f.id in anchor_moved_finding_ids
                }

                for finding in snapshot_agg.open_findings_in_files(touched_files):
                    if finding.id in anchor_result.gone:
                        # Anchor gone — already marked resolved_unverified on
                        # the snapshot; persisted when we save the live
                        # aggregate below.
                        continue
                    stale_ctx = _stale_check_context_for(finding, diff)
                    stale_result = await coding_agent.stale_check(
                        plugin_id=_CODING_AGENT_PLUGIN_ID,
                        workspace=ws,
                        context=stale_ctx,
                    )
                    if stale_result.status == InvocationStatus.SUCCESS:
                        stale_results.append((finding.id, stale_result))
                    else:
                        log.info(
                            "incremental.stale_check_failed",
                            finding_id=str(finding.id),
                            status=str(stale_result.status),
                        )

            # Plan §2.3: anchor + fingerprint hashes need real file content.
            # Pre-load file contents for each new draft's anchor path while the
            # workspace is still mounted; reused outside the workspace block.
            new_finding_contents: dict[str, list[str] | None] = {}
            for draft in result.findings or []:
                fp = draft.anchor.file_path
                if fp in new_finding_contents:
                    continue
                text = await ws.read_text(fp)
                new_finding_contents[fp] = None if text is None else text.splitlines()

        if result.status != InvocationStatus.SUCCESS:
            await _fail_review(review_id, result.error_message or f"agent status={result.status}")
            return

        await _set_step(review_id, "posting_review", pr_id=pr.id)
        async with db_session() as s:
            await acquire_pr_lock(s, pr.id)
            agg_repo = SqlAlchemyAggregateRepository(s)
            aggregate = await agg_repo.load(pr_id=pr.id, org_id=org_id)

            # Replay the anchor pass onto the live aggregate (the snapshot
            # used inside `with_workspace` was a separate instance).
            _live_finding_ids = {f.id for f in aggregate.findings}
            for moved_id, snap in anchor_moved_snapshots.items():
                if moved_id in _live_finding_ids:
                    aggregate.update_anchor(moved_id, snap.current_anchor)
            for gone_id in anchor_gone_finding_ids:
                if gone_id in _live_finding_ids:
                    aggregate.mark_unverified_resolution(gone_id)

            raw = _findingdrafts_to_raw(
                result.findings,
                commit_sha=head_sha,
                read_file=new_finding_contents.get,
            )
            # Plan §10.9: drop findings whose anchor file isn't in this push's diff.
            new_findings, _obs, drops = aggregate.post_process_raw_findings(
                review_id, raw, diff_files=touched_files
            )

            # Post each new finding as a fresh yaaos comment via vcs.post_review.
            if new_findings:
                review_obj = Review(
                    agent_tag=_REVIEWER_TAG,
                    state="COMMENT",
                    summary_body=None,
                    findings=_raw_to_vcs_findings(raw, new_findings),
                )
                post_result = await vcs_plugin.post_review(pr.external_id, review_obj)
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

            # Apply the stale_check results captured inside the workspace block.
            for finding_id, sr in stale_results:
                action = apply_stale_check_result(
                    aggregate,
                    finding_id=finding_id,
                    still_applies=sr.still_applies,
                    confidence=sr.confidence,
                )
                if action.kind == "stale_marked" and action.reply_body:
                    thread = aggregate.thread_for_finding(finding_id)
                    if thread is None:
                        continue
                    parent_external = next(
                        (
                            m.external_comment_id
                            for m in reversed(aggregate.messages)
                            if m.thread_id == thread.id
                        ),
                        None,
                    )
                    if parent_external is None:
                        continue
                    try:
                        yaaos_comment_id = await vcs_plugin.post_comment_reply(
                            pr.external_id, parent_external, action.reply_body
                        )
                    except Exception:
                        log.exception("incremental.stale_post_reply_failed", finding_id=str(finding_id))
                        yaaos_comment_id = f"local-stale-{finding_id}"
                    aggregate.append_message(
                        thread_id=thread.id,
                        author_kind="yaaos",
                        author_external_id=_REVIEWER_TAG,
                        external_comment_id=yaaos_comment_id,
                        body=action.reply_body,
                        in_reply_to_external_id=parent_external,
                    )

            aggregate.complete_review(review_id, [f.id for f in new_findings])
            await agg_repo.save(aggregate)
            await dispatch_audits(aggregate, session=s, actor=Actor.system(), org_id=org_id)
            await dispatch_events(aggregate)

            if drops:
                log.info(
                    "incremental.admission_drops",
                    review_id=str(review_id),
                    drops=[
                        {
                            "rule_id": d.rule_id,
                            "reason": d.reason,
                            "severity": d.severity,
                            "confidence": d.confidence,
                        }
                        for d in drops
                    ],
                )

            await s.execute(
                update(ReviewRow)
                .where(ReviewRow.id == review_id)
                .values(
                    status="posted",
                    completed_at=_utcnow(),
                    current_step="posted",
                    tokens_in=result.telemetry.tokens_in,
                    tokens_out=result.telemetry.tokens_out,
                )
            )
            await s.commit()

        await publish(ReviewJobStatusChanged(pr_id=pr.id, review_job_id=review_id, status="posted"))

        # Trigger-policy step 4: if pending_replay was set while we were
        # running, re-evaluate the trigger so a queued push gets picked up.
        async with db_session() as s:
            current = (
                await s.execute(select(ReviewRow).where(ReviewRow.id == review_id))
            ).scalar_one_or_none()
        if current and current.pending_replay:
            await handle_push(pr.id, new_head_sha=head_sha, prev_head_sha=prev_sha, org_id=org_id)

    except Exception as e:
        log.exception("incremental.crashed", review_id=str(review_id))
        await _fail_review(review_id, f"handler crashed: {e}")


@dataclass
class ResolveAnchorsResult:
    """What `resolve_open_anchors` did, partitioned by outcome.

    - `moved`: finding ids whose anchor block now lives at a new line range.
      The caller should run verify_fix on these (plan §6.2 step 4b).
    - `gone`: finding ids whose surrounding hash isn't in the new content
      (file deleted, or block removed/heavily edited). Aggregate state is
      already set to `resolved_unverified`; LLM stale_check can still
      transition further if it knows better.
    - `unchanged`: finding ids whose anchor still sits at the same line
      range (commit_sha was refreshed for bookkeeping).
    """

    moved: list[UUID] = field(default_factory=list)
    gone: list[UUID] = field(default_factory=list)
    unchanged: list[UUID] = field(default_factory=list)


def resolve_open_anchors(
    aggregate: _Aggregate,
    *,
    touched_files: set[str],
    read_file: Callable[[str], list[str] | None],
    new_commit_sha: str,
) -> ResolveAnchorsResult:
    """Re-resolve anchors for every open finding in `touched_files`.

    Pure helper — no I/O of its own. The caller supplies `read_file` (which
    inside the incremental runner reads from the workspace; in tests is a
    pure dict lookup). Plan §6.2 step 4b: deterministic anchor lookup before
    the LLM stale_check fires.
    """
    out = ResolveAnchorsResult()
    for finding in aggregate.open_findings_in_files(touched_files):
        new_lines = read_file(finding.current_anchor.file_path)
        if new_lines is None:
            aggregate.mark_unverified_resolution(finding.id)
            out.gone.append(finding.id)
            continue
        new_anchor = resolve_anchor(finding.current_anchor, new_lines, new_commit_sha)
        if new_anchor is None:
            aggregate.mark_unverified_resolution(finding.id)
            out.gone.append(finding.id)
            continue
        if (
            new_anchor.line_start == finding.current_anchor.line_start
            and new_anchor.line_end == finding.current_anchor.line_end
        ):
            out.unchanged.append(finding.id)
            continue
        aggregate.update_anchor(finding.id, new_anchor)
        out.moved.append(finding.id)
    return out


def _stale_check_context_for(finding: Any, diff: Any) -> StaleCheckContext:
    """Build a StaleCheckContext from a Finding + the incremental diff.

    The agent has the workspace and reads the file itself, so we hand it the
    finding metadata and a brief diff summary. No file content here.
    """
    file_path = finding.current_anchor.file_path
    matched = next((f for f in (diff.files or []) if f.path == file_path), None)
    summary = (
        f"{file_path}: +{matched.additions}/-{matched.deletions} lines"
        if matched is not None
        else f"{file_path}: not in this diff"
    )
    return StaleCheckContext(
        original_finding_title=finding.title,
        original_finding_body=finding.body,
        original_rule_id=finding.rule_id,
        current_code_snippet=f"see {file_path} at lines {finding.current_anchor.line_start}-{finding.current_anchor.line_end}",
        diff_summary=summary,
        agent_config={},
    )


# `_findingdrafts_to_raw` + `_raw_to_vcs_findings` moved to queue.py and
# are now shared between the full-review path and the incremental path.


async def _fail_review(review_id: UUID, error: str) -> None:
    async with db_session() as s:
        await s.execute(
            update(ReviewRow)
            .where(ReviewRow.id == review_id)
            .values(status="failed", completed_at=_utcnow(), error_message=error, current_step="failed")
        )
        await s.commit()


async def _skip_review(review_id: UUID, reason: str) -> None:
    async with db_session() as s:
        await s.execute(
            update(ReviewRow)
            .where(ReviewRow.id == review_id)
            .values(status="skipped", skip_reason=reason, completed_at=_utcnow())
        )
        await s.commit()


# ── Trigger-input helpers ─────────────────────────────────────────────────────


async def _last_reviewed_sha(pr_id: UUID) -> str | None:
    """The commit_sha_at_start of the most-recent posted review for this PR."""
    async with db_session() as s:
        row = (
            await s.execute(
                select(ReviewRow.commit_sha_at_start)
                .where(ReviewRow.pr_id == pr_id, ReviewRow.status == "posted")
                .order_by(desc(ReviewRow.created_at))
                .limit(1)
            )
        ).first()
    return row[0] if row else None


async def _in_flight_review_id(pr_id: UUID) -> UUID | None:
    async with db_session() as s:
        row = (
            await s.execute(
                select(ReviewRow.id)
                .where(ReviewRow.pr_id == pr_id, ReviewRow.status.in_(["queued", "running"]))
                .limit(1)
            )
        ).first()
    return row[0] if row else None


async def _last_push_timestamp(pr_id: UUID) -> datetime | None:
    """When was the last incremental review scheduled? Used for debounce.

    POC: best-effort. We approximate with the most recent ReviewRow scheduled_at
    where trigger_reason is push-related. A future GitHub-side push timestamp
    is more accurate but not wired yet.
    """
    async with db_session() as s:
        row = (
            await s.execute(
                select(ReviewRow.scheduled_at)
                .where(
                    ReviewRow.pr_id == pr_id,
                    ReviewRow.trigger_reason.in_(["push_incremental", "pr_synchronized"]),
                )
                .order_by(desc(ReviewRow.scheduled_at))
                .limit(1)
            )
        ).first()
    return row[0] if row else None


async def _new_commit_messages(
    plugin_id: str, repo_external_id: str, prev_sha: str | None, head_sha: str
) -> list[str]:
    """Fetch commit messages between `prev_sha` and `head_sha` via the VCS plugin.

    Used by the trigger policy to detect base-branch merges (a commit whose
    message starts with `Merge branch '...' into ...`). Returns `[]` if the
    plugin doesn't expose a compare API or the call fails; the base-merge
    heuristic then simply doesn't fire.
    """
    if not prev_sha or prev_sha == head_sha:
        return []
    plugin = get_vcs_plugin(plugin_id)
    try:
        return await plugin.list_commit_messages(repo_external_id, prev_sha, head_sha)
    except Exception:
        log.warning(
            "incremental.list_commit_messages_failed",
            repo=repo_external_id,
            prev=prev_sha,
            head=head_sha,
        )
        return []


async def _is_ancestor(plugin_id: str, repo_external_id: str, prev_sha: str | None, head_sha: str) -> bool:
    """True iff `prev_sha` is an ancestor of `head_sha` (no force-push between them).

    Uses the VCS plugin's `detect_force_push` (compare-API `status=="diverged"`
    inverted). When `prev_sha` is None or the API call fails, falls back to
    `False` so the trigger policy routes to `Skip("history_changed")` and
    the user routes through manual full re-review.
    """
    if not prev_sha:
        return False
    if prev_sha == head_sha:
        return True
    plugin = get_vcs_plugin(plugin_id)
    try:
        force_push = await plugin.detect_force_push(repo_external_id, prev_sha, head_sha)
        return not force_push
    except Exception:
        log.warning("incremental.is_ancestor_failed", repo=repo_external_id, prev=prev_sha, head=head_sha)
        return False


__all__ = ["handle_push"]
