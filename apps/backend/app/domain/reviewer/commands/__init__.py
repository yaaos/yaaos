"""Reviewer WorkflowCommands for the five task modes.

Five **Workspace** commands wrap `domain/coding_agent` invocations against
a workspace:
- `CodeReview` — full-PR review.
- `IncrementalReview` — push-driven incremental review against a base sha.
- `VerifyFix` — ack a developer's "is this fixed?" reply on a finding.
- `StaleCheck` — periodic check that an open finding still applies.
- `AnswerQuestion` — answer a developer @yaaos-mention on a finding.

Five **Local** commands handle the control-plane side:
- `CheckShouldReview` — admission gating (draft/skip-label/external-contrib/
  org-config) before any workspace is provisioned.
- `PostFindings` — admit findings via the aggregate, post to GitHub.
- `ResolveFinding` — close a finding's thread on a verified fix.
- `ArchiveStaleFindings` — mark stale findings archived.
- `PostReply` — post a reply on a finding's thread.

All 10 commands ship with real bodies. Local-category bodies persist via
the reviewer aggregate + post via the registered VCS plugin; Workspace
bodies resolve their workspace + ticket context, then call the matching
`domain/coding_agent.<method>`. See [`domain_reviewer.md`](../../../docs/domain_reviewer.md)
for per-command output shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select, update

from app.core.database import session as db_session
from app.core.workflow import CommandCategory, CommandContext, Outcome
from app.core.workspace import (
    Workspace,
    WorkspaceTicketContext,
    get_workflow_context_provider,
    get_workspace,
)
from app.domain.tickets import get_payload as get_ticket_payload

log = structlog.get_logger("domain.reviewer.commands")

# Labels whose presence on a PR force-skips the review. Matches the legacy
# `queue.py` behavior so the cutover is a straight swap. Case-insensitive.
SKIP_LABELS: frozenset[str] = frozenset({"yaaos-skip", "no-review", "wip"})

# ── Workspace commands (5) ──────────────────────────────────────────────


class _WorkspaceReviewCommand:
    """Workspace-category reviewer command. The base does three things on
    every invocation:

    1. Reads `workspace_id` from inputs and resolves it to a live `Workspace`
       handle via `core/workspace.get_workspace()`. Missing or unresolved →
       `Outcome.failure` (the upstream `ProvisionWorkspace` step would have
       failed to write `workspace_id` into outputs, or the row was destroyed
       between provision and review).
    2. Fetches the `WorkspaceTicketContext` (org_id, plugin_id, repo,
       payload, pr_id) for the workflow's ticket via the registered
       `WorkflowContextProvider`. Missing provider or missing ticket → also
       `Outcome.failure` — the workflow can't proceed without org context
       to look up plugins / build review context.
    3. Hands the resolved (workspace, ticket_ctx, inputs, ctx) to the
       subclass via `_run_in_workspace(...)`.

    Subclass bodies (`CodeReview`, `IncrementalReview`, `VerifyFix`,
    `StaleCheck`, `AnswerQuestion`) override `_run_in_workspace` to build
    their `domain/coding_agent` context and invoke the matching agent
    method.
    """

    category = CommandCategory.WORKSPACE
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        ws_id_raw = inputs.get("workspace_id")
        if not ws_id_raw:
            return Outcome.failure(reason="missing workspace_id input")
        try:
            ws_id = UUID(str(ws_id_raw))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid workspace_id: {ws_id_raw!r}")

        workspace = await get_workspace(ws_id)
        if workspace is None:
            return Outcome.failure(reason=f"workspace {ws_id} not resolvable")

        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")

        try:
            ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        except Exception as exc:
            log.exception(
                "workspace_review.context_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        if ticket_ctx is None:
            return Outcome.failure(reason=f"ticket {ctx.ticket_id} not found")

        return await self._run_in_workspace(workspace, ticket_ctx, inputs, ctx)

    async def _run_in_workspace(
        self,
        workspace: Workspace,
        ticket_ctx: WorkspaceTicketContext,
        inputs: dict[str, Any],
        ctx: CommandContext,
    ) -> Outcome:
        """Override in subclasses to invoke the relevant `domain/coding_agent`
        method against the live workspace. Default body is success — keeps
        the engine workflow draining cleanly until each real body lands."""
        del workspace, ticket_ctx, inputs, ctx
        return Outcome.success()


async def _load_finding_by_id(pr_id: UUID, org_id: UUID, finding_id: UUID):  # type: ignore[no-untyped-def]
    """Helper for the three Workspace bodies that operate on a single finding
    (VerifyFix, StaleCheck, AnswerQuestion). Loads the reviewer aggregate
    and returns the named Finding, or None if not present."""
    from app.domain.reviewer.repository import SqlAlchemyAggregateRepository  # noqa: PLC0415

    async with db_session() as s:
        repo = SqlAlchemyAggregateRepository(s)
        aggregate = await repo.load(pr_id=pr_id, org_id=org_id)
    for finding in aggregate.findings:
        if finding.id == finding_id:
            return finding
    return None


def _activity_publisher_for(ctx: CommandContext):  # type: ignore[no-untyped-def]
    """Build an `on_activity` callback that fan-outs each `ActivityEvent`
    to `core/sse_pubsub` on `channel_for(ctx.workflow_execution_id)`.

    Hands the in-memory workspace path the same SSE feed the remote-agent
    path gets — the SPA's `/api/workflows/{id}/activity` consumer sees
    events whether the workflow runs inline or via a wire-dispatched
    AgentCommand.

    Best-effort: publish failures are caught + logged so a flaky pubsub
    backend doesn't sink the review. Tests that don't subscribe just
    drop the events (the InMemoryPubsub no-ops when no subscribers).
    """

    async def _publisher(event):  # type: ignore[no-untyped-def]
        from app.core.sse_pubsub import channel_for  # noqa: PLC0415
        from app.core.sse_pubsub import publish as sse_publish  # noqa: PLC0415

        try:
            await sse_publish(
                channel_for(ctx.workflow_execution_id),
                event.model_dump(mode="json"),
            )
        except Exception:
            log.exception(
                "workspace_review.activity_publish_failed",
                workflow_execution_id=ctx.workflow_execution_id,
            )

    return _publisher


async def _read_code_snippet_at_anchor(
    workspace: Workspace, file_path: str, line_start: int, line_end: int
) -> str:
    """Read the code lines at the anchor. Empty string if the file is gone."""
    text = await workspace.read_text(file_path)
    if not text:
        return ""
    lines = text.splitlines()
    start = max(0, line_start - 1)
    end = min(len(lines), line_end)
    return "\n".join(lines[start:end])


class CodeReview(_WorkspaceReviewCommand):
    """Full-PR review. Invokes `coding_agent.review` against the workspace
    with a `ReviewContext` built from the ticket payload + PR row + diff.

    NOT included in the in-memory fast path: building the full ReviewContext
    requires `pr` (VCSPullRequest) and `diff` (Diff) — both heavy and
    typically fetched from the vcs plugin. For the POC in-memory path, this
    body builds a minimal `ReviewContext` from the ticket payload + repo
    info and lets the coding-agent plugin handle the rest. Tests can
    register a stub coding-agent plugin to assert the wiring.

    Outputs `draft_findings: list[dict]` for downstream PostFindings.
    """

    kind = "CodeReview"

    async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs
        from app.domain import coding_agent  # noqa: PLC0415
        from app.domain.coding_agent import ReviewContext  # noqa: PLC0415
        from app.domain.vcs import Diff, VCSPullRequest  # noqa: PLC0415

        # Minimal PR + diff for the POC in-memory path. Real production code
        # would fetch via vcs.get_pr / vcs.get_diff; that wiring lands with
        # the Phase 6 Go subprocess body which has the real VCS side.
        head_sha = str(ticket_ctx.payload.get("head_sha") or "")
        base_sha = str(ticket_ctx.payload.get("base_sha") or "")
        pr_external_id = str(ticket_ctx.payload.get("pr_external_id") or "")
        now = datetime.now(UTC)
        author_type_raw = str(ticket_ctx.payload.get("author_type") or "user")
        author_type: Any = author_type_raw if author_type_raw in ("user", "bot") else "user"
        state_raw = str(ticket_ctx.payload.get("state") or "open")
        state: Any = state_raw if state_raw in ("open", "closed", "merged") else "open"
        try:
            pr = VCSPullRequest(
                plugin_id=ticket_ctx.plugin_id,
                external_id=pr_external_id,
                repo_external_id=ticket_ctx.repo_external_id,
                number=int(ticket_ctx.payload.get("pr_number") or 0),
                title=str(ticket_ctx.payload.get("title") or ""),
                body=str(ticket_ctx.payload.get("body") or ""),
                author_login=str(ticket_ctx.payload.get("author_login") or ""),
                author_type=author_type,
                base_branch=str(ticket_ctx.payload.get("base_branch") or "main"),
                head_branch=str(ticket_ctx.payload.get("head_branch") or ""),
                base_sha=base_sha,
                head_sha=head_sha,
                is_draft=bool(ticket_ctx.payload.get("is_draft", False)),
                is_fork=bool(ticket_ctx.payload.get("is_fork", False)),
                state=state,
                html_url=str(ticket_ctx.payload.get("html_url") or ""),
                created_at=now,
                updated_at=now,
            )
        except Exception as exc:
            return Outcome.failure(reason=f"could not build VCSPullRequest: {exc}")
        diff = Diff(raw="", files=[])

        review_ctx = ReviewContext(pr=pr, diff=diff, language_hint=None)
        try:
            result = await coding_agent.review(
                "claude_code", workspace, review_ctx, on_activity=_activity_publisher_for(ctx)
            )
        except Exception as exc:
            log.exception(
                "code_review.coding_agent_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        return Outcome.success(
            outputs={
                "draft_findings": [f.model_dump(mode="json") for f in result.findings],
                "summary_body": result.summary_body or "",
                "state": result.state or "COMMENT",
            }
        )


class IncrementalReview(_WorkspaceReviewCommand):
    """Push-driven incremental review against `prev_sha..head_sha`.

    Full body — provisions nothing (the prior `ProvisionWorkspace` step
    owns the workspace), but does everything else `run_incremental_review`
    used to do in the legacy `incremental.py`: fetches the real diff +
    lessons + prior-finding summaries, builds the IncrementalReviewContext,
    invokes `coding_agent.incremental_review`, runs the deterministic
    anchor pass + LLM stale-check on touched-file open findings, posts the
    new findings + stale-check replies via the VCS plugin, persists via
    the aggregate, and updates the legacy `ReviewRow` for the SPA's
    per-PR history view. On `pending_replay`, re-triggers via
    `start_incremental_review`.

    Inputs: `workspace_id` (from ProvisionWorkspace). Reads from the
    ticket payload (set by `start_incremental_review`): `review_id`,
    `prev_sha`, `head_sha`, `pr_external_id`.

    Outputs: `review_id`, `admitted_count`, `dropped_count`,
    `anchor_moved`, `anchor_gone`, `stale_marked`. PostFindings runs as a
    no-op (we already posted inline — atomic with the anchor/stale
    aggregate mutations).
    """

    kind = "IncrementalReview"

    async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs
        from app.core.audit_log import Actor  # noqa: PLC0415
        from app.core.database import session as db_session  # noqa: PLC0415
        from app.domain import coding_agent, lessons, pull_requests  # noqa: PLC0415
        from app.domain.coding_agent import (  # noqa: PLC0415
            IncrementalReviewContext,
            InvocationStatus,
        )
        from app.domain.reviewer.admission import (  # noqa: PLC0415
            findingdrafts_to_raw,
            raw_to_vcs_findings,
        )
        from app.domain.reviewer.constants import (  # noqa: PLC0415
            CODING_AGENT_PLUGIN_ID,
            REVIEWER_TAG,
        )
        from app.domain.reviewer.diff_utils import (  # noqa: PLC0415
            detect_language,
            ticket_skip_reason,
        )
        from app.domain.reviewer.incremental_anchor import (  # noqa: PLC0415
            resolve_open_anchors,
            stale_check_context_for,
        )
        from app.domain.reviewer.incremental_trigger import (  # noqa: PLC0415
            fail_review,
            set_review_step,
            skip_review,
            start_incremental_review,
        )
        from app.domain.reviewer.lock import acquire_pr_lock  # noqa: PLC0415
        from app.domain.reviewer.models import ReviewRow  # noqa: PLC0415
        from app.domain.reviewer.repository import (  # noqa: PLC0415
            SqlAlchemyAggregateRepository,
        )
        from app.domain.reviewer.service import (  # noqa: PLC0415
            apply_stale_check_result,
            dispatch_audits,
            dispatch_events,
        )
        from app.domain.reviewer.types import FindingState  # noqa: PLC0415
        from app.domain.vcs import Review  # noqa: PLC0415
        from app.domain.vcs import get_plugin as get_vcs_plugin  # noqa: PLC0415

        payload = ticket_ctx.payload or {}
        review_id_raw = payload.get("review_id")
        if not review_id_raw:
            return Outcome.failure(reason="missing review_id in ticket payload")
        try:
            review_id = UUID(str(review_id_raw))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid review_id: {review_id_raw!r}")

        prev_sha = str(payload.get("prev_sha") or payload.get("base_sha") or "")
        head_sha = str(payload.get("head_sha") or "")
        if not head_sha:
            await fail_review(review_id, "missing head_sha")
            return Outcome.failure(reason="missing head_sha in payload")

        if ticket_ctx.pr_id is None:
            await fail_review(review_id, "ticket has no PR")
            return Outcome.failure(reason="ticket has no PR")

        org_id = ticket_ctx.org_id
        try:
            pr = await pull_requests.get(ticket_ctx.pr_id, org_id=org_id)
        except Exception as exc:
            await fail_review(review_id, f"pr_fetch_failed: {exc}")
            return Outcome.failure(reason=f"pr fetch failed: {exc}")

        vcs_plugin = get_vcs_plugin(pr.plugin_id)
        try:
            vcs_pr = await vcs_plugin.fetch_pr(pr.external_id)
            diff = await vcs_plugin.fetch_diff(pr.external_id)
        except Exception as exc:
            await fail_review(review_id, f"vcs_fetch_failed: {exc}")
            return Outcome.failure(reason=f"vcs fetch failed: {exc}")

        lesson_rows = await lessons.list_for_repo(pr.repo_external_id, org_id=org_id, plugin_id=pr.plugin_id)
        language = detect_language(diff)

        async with db_session() as s:
            from datetime import UTC, datetime  # noqa: PLC0415

            await s.execute(
                update(ReviewRow)
                .where(ReviewRow.id == review_id)
                .values(
                    status="running",
                    started_at=datetime.now(UTC),
                    current_step="resolving_entities",
                )
            )
            await s.commit()

        skip_reason = ticket_skip_reason(pr, diff)
        if skip_reason is not None:
            await skip_review(review_id, skip_reason)
            return Outcome.success(label="skip", outputs={"reason": skip_reason})

        # Prior findings for the prompt.
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

        await set_review_step(review_id, "invoking_agent")
        review_ctx = IncrementalReviewContext(
            pr=vcs_pr,
            diff=diff,
            prev_sha=prev_sha,
            head_sha=head_sha,
            lessons=lesson_rows,
            language_hint=language,
            prior_open_finding_summaries=prior_open,
            prior_acknowledged_finding_summaries=prior_ack,
            agent_config={},
        )

        try:
            result = await coding_agent.incremental_review(
                plugin_id=CODING_AGENT_PLUGIN_ID,
                workspace=workspace,
                context=review_ctx,
                on_activity=_activity_publisher_for(ctx),
            )
        except Exception as exc:
            log.exception(
                "incremental_review.coding_agent_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            await fail_review(review_id, f"{type(exc).__name__}: {exc}")
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        # Anchor pass + stale check while the workspace is still mounted.
        touched_files = {f.path for f in diff.files} if diff.files else set()
        stale_results: list[tuple[UUID, Any]] = []
        anchor_moved_finding_ids: set[UUID] = set()
        anchor_gone_finding_ids: set[UUID] = set()
        anchor_moved_snapshots: dict[UUID, Any] = {}

        if touched_files:
            async with db_session() as snapshot_s:
                snapshot_repo = SqlAlchemyAggregateRepository(snapshot_s)
                snapshot_agg = await snapshot_repo.load(pr_id=pr.id, org_id=org_id)

            files_needed = {
                f.current_anchor.file_path for f in snapshot_agg.open_findings_in_files(touched_files)
            }
            contents: dict[str, list[str] | None] = {}
            for fp in files_needed:
                text = await workspace.read_text(fp)
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
                    continue
                stale_ctx = stale_check_context_for(finding, diff)
                stale_result = await coding_agent.stale_check(
                    plugin_id=CODING_AGENT_PLUGIN_ID,
                    workspace=workspace,
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

        # Read file contents for new draft anchors before workspace teardown.
        new_finding_contents: dict[str, list[str] | None] = {}
        for draft in result.findings or []:
            fp = draft.anchor.file_path
            if fp in new_finding_contents:
                continue
            text = await workspace.read_text(fp)
            new_finding_contents[fp] = None if text is None else text.splitlines()

        if result.status != InvocationStatus.SUCCESS:
            await fail_review(review_id, result.error_message or f"agent status={result.status}")
            return Outcome.failure(reason=result.error_message or f"agent status={result.status}")

        await set_review_step(review_id, "posting_review")

        stale_marked = 0
        async with db_session() as s:
            await acquire_pr_lock(s, pr.id)
            agg_repo = SqlAlchemyAggregateRepository(s)
            aggregate = await agg_repo.load(pr_id=pr.id, org_id=org_id)

            live_finding_ids = {f.id for f in aggregate.findings}
            for moved_id, snap in anchor_moved_snapshots.items():
                if moved_id in live_finding_ids:
                    aggregate.update_anchor(moved_id, snap.current_anchor)
            for gone_id in anchor_gone_finding_ids:
                if gone_id in live_finding_ids:
                    aggregate.mark_unverified_resolution(gone_id)

            raw = findingdrafts_to_raw(
                result.findings,
                commit_sha=head_sha,
                read_file=new_finding_contents.get,
            )
            new_findings, _obs, drops = aggregate.post_process_raw_findings(
                review_id, raw, diff_files=touched_files
            )

            if new_findings:
                review_obj = Review(
                    agent_tag=REVIEWER_TAG,
                    state="COMMENT",
                    summary_body=None,
                    findings=raw_to_vcs_findings(raw, new_findings),
                )
                post_result = await vcs_plugin.post_review(pr.external_id, review_obj)
                external_ids = list(post_result.finding_to_comment_external_id.values())
                for idx, f in enumerate(new_findings):
                    external_id = external_ids[idx] if idx < len(external_ids) else f"local-{f.id}"
                    thread = aggregate.open_thread_for_finding(f.id)
                    aggregate.append_message(
                        thread_id=thread.id,
                        author_kind="yaaos",
                        author_external_id=REVIEWER_TAG,
                        external_comment_id=external_id,
                        body=f.body,
                    )

            for finding_id, sr in stale_results:
                action = apply_stale_check_result(
                    aggregate,
                    finding_id=finding_id,
                    still_applies=sr.still_applies,
                    confidence=sr.confidence,
                )
                if action.kind == "stale_marked" and action.reply_body:
                    stale_marked += 1
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
                        author_external_id=REVIEWER_TAG,
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

            from datetime import UTC, datetime  # noqa: PLC0415

            await s.execute(
                update(ReviewRow)
                .where(ReviewRow.id == review_id)
                .values(
                    status="posted",
                    completed_at=datetime.now(UTC),
                    current_step="posted",
                    tokens_in=result.telemetry.tokens_in,
                    tokens_out=result.telemetry.tokens_out,
                )
            )
            await s.commit()

        # Trigger-policy §7 rule 4: re-evaluate if pending_replay was set.
        async with db_session() as s:
            current = (
                await s.execute(select(ReviewRow).where(ReviewRow.id == review_id))
            ).scalar_one_or_none()
        if current and current.pending_replay:
            await start_incremental_review(
                pr.id, new_head_sha=head_sha, prev_head_sha=prev_sha, org_id=org_id
            )

        # Returns empty draft_findings so the downstream PostFindings step
        # is a no-op (posting + admission already happened atomically with
        # the aggregate persist above — preserves the anchor/post invariant).
        return Outcome.success(
            outputs={
                "review_id": str(review_id),
                "draft_findings": [],
                "admitted_count": len(new_findings),
                "dropped_count": len(drops),
                "anchor_moved": len(anchor_moved_finding_ids),
                "anchor_gone": len(anchor_gone_finding_ids),
                "stale_marked": stale_marked,
            }
        )


class VerifyFix(_WorkspaceReviewCommand):
    """Verify whether a previously-raised finding is still present at HEAD.
    Loads the finding by `finding_id`, reads the current code at its anchor,
    invokes `coding_agent.verify_fix`. Output is a `verdict` dict that
    `ResolveFinding` consumes."""

    kind = "VerifyFix"

    async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
        finding_id_raw = inputs.get("finding_id")
        if not finding_id_raw:
            return Outcome.failure(reason="missing finding_id input")
        try:
            finding_id = UUID(str(finding_id_raw))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid finding_id: {finding_id_raw!r}")
        if ticket_ctx.pr_id is None:
            return Outcome.failure(reason="ticket has no pr_id")
        finding = await _load_finding_by_id(ticket_ctx.pr_id, ticket_ctx.org_id, finding_id)
        if finding is None:
            return Outcome.success(outputs={"verdict": {"finding_id": str(finding_id), "skipped": "unknown"}})

        from app.domain import coding_agent  # noqa: PLC0415
        from app.domain.coding_agent import FindingAnchor, VerifyFixContext  # noqa: PLC0415

        current_code = await _read_code_snippet_at_anchor(
            workspace,
            finding.current_anchor.file_path,
            finding.current_anchor.line_start,
            finding.current_anchor.line_end,
        )
        original_code = "\n".join(finding.current_anchor.original_lines or [])

        vctx = VerifyFixContext(
            original_finding_title=finding.title,
            original_finding_body=finding.body,
            original_rule_id=finding.rule_id,
            original_code_snippet=original_code,
            current_code_snippet=current_code,
            current_anchor=FindingAnchor(
                file_path=finding.current_anchor.file_path,
                line_start=finding.current_anchor.line_start,
                line_end=finding.current_anchor.line_end,
            ),
        )
        try:
            result = await coding_agent.verify_fix(
                "claude_code", workspace, vctx, on_activity=_activity_publisher_for(ctx)
            )
        except Exception as exc:
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        return Outcome.success(
            outputs={
                "verdict": {
                    "finding_id": str(finding_id),
                    "still_present": result.still_present,
                    "confidence": result.confidence,
                    "reasoning": result.reasoning,
                }
            }
        )


class StaleCheck(_WorkspaceReviewCommand):
    """Check whether each finding in `finding_ids` still meaningfully applies
    after code changes. Loops over the input list and accumulates ids whose
    `still_applies=False` verdicts pass the confidence threshold (≥ 0.80)
    — those go to `ArchiveStaleFindings` via `stale_finding_ids` output."""

    kind = "StaleCheck"

    async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
        ids_raw = inputs.get("finding_ids") or []
        if not ids_raw or ticket_ctx.pr_id is None:
            return Outcome.success(outputs={"stale_finding_ids": []})

        from app.domain import coding_agent  # noqa: PLC0415
        from app.domain.coding_agent import StaleCheckContext  # noqa: PLC0415

        stale_ids: list[str] = []
        for raw in ids_raw:
            try:
                fid = UUID(str(raw))
            except (TypeError, ValueError):
                continue
            finding = await _load_finding_by_id(ticket_ctx.pr_id, ticket_ctx.org_id, fid)
            if finding is None:
                continue
            current_code = await _read_code_snippet_at_anchor(
                workspace,
                finding.current_anchor.file_path,
                finding.current_anchor.line_start,
                finding.current_anchor.line_end,
            )
            sctx = StaleCheckContext(
                original_finding_title=finding.title,
                original_finding_body=finding.body,
                original_rule_id=finding.rule_id,
                current_code_snippet=current_code,
                diff_summary="",
            )
            try:
                result = await coding_agent.stale_check(
                    "claude_code", workspace, sctx, on_activity=_activity_publisher_for(ctx)
                )
            except Exception:
                log.exception("stale_check.coding_agent_failed", finding_id=str(fid))
                continue
            if not result.still_applies and result.confidence >= 0.80:
                stale_ids.append(str(fid))

        return Outcome.success(outputs={"stale_finding_ids": stale_ids})


class AnswerQuestion(_WorkspaceReviewCommand):
    """Answer a developer @yaaos-mention question on a finding. Loads the
    finding, reads code at its anchor, invokes `coding_agent.answer_question`,
    returns the reply body for downstream `PostReply`."""

    kind = "AnswerQuestion"

    async def _run_in_workspace(self, workspace, ticket_ctx, inputs, ctx):  # type: ignore[no-untyped-def]
        finding_id_raw = inputs.get("finding_id")
        question = inputs.get("question_body") or ""
        if not finding_id_raw or not question:
            return Outcome.success(outputs={"reply_body": "", "finding_id": None})
        try:
            finding_id = UUID(str(finding_id_raw))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid finding_id: {finding_id_raw!r}")
        if ticket_ctx.pr_id is None:
            return Outcome.success(outputs={"reply_body": "", "finding_id": str(finding_id)})
        finding = await _load_finding_by_id(ticket_ctx.pr_id, ticket_ctx.org_id, finding_id)
        if finding is None:
            return Outcome.success(outputs={"reply_body": "", "finding_id": str(finding_id)})

        from app.domain import coding_agent  # noqa: PLC0415
        from app.domain.coding_agent import AnswerQuestionContext, FindingAnchor  # noqa: PLC0415

        code_snippet = await _read_code_snippet_at_anchor(
            workspace,
            finding.current_anchor.file_path,
            finding.current_anchor.line_start,
            finding.current_anchor.line_end,
        )
        actx = AnswerQuestionContext(
            original_finding_title=finding.title,
            original_finding_body=finding.body,
            original_rule_id=finding.rule_id,
            code_snippet=code_snippet,
            current_anchor=FindingAnchor(
                file_path=finding.current_anchor.file_path,
                line_start=finding.current_anchor.line_start,
                line_end=finding.current_anchor.line_end,
            ),
            question=str(question),
            base_sha=str(ticket_ctx.payload.get("base_sha") or ""),
            head_sha=str(ticket_ctx.payload.get("head_sha") or ""),
        )
        try:
            result = await coding_agent.answer_question(
                "claude_code", workspace, actx, on_activity=_activity_publisher_for(ctx)
            )
        except Exception as exc:
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        return Outcome.success(
            outputs={
                "reply_body": result.answer,
                "finding_id": str(finding_id),
            }
        )


# ── Local commands (5) ──────────────────────────────────────────────────


class _LocalReviewCommand:
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


class CheckShouldReview:
    """Admission gate before provisioning. Returns `Outcome.success(label='skip')`
    when the PR is draft / fork / bot-authored / skip-labelled; workflow
    then terminates without spinning up a workspace. The PR payload (set by
    `plugins/github/intake_type`) carries `is_draft`, `is_fork`, `labels`,
    `author_login`."""

    kind = "CheckShouldReview"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs
        async with db_session() as s:
            payload = await get_ticket_payload(UUID(ctx.ticket_id), session=s)

        reason = _decide_skip(payload)
        if reason is not None:
            log.info(
                "checkshouldreview.skip",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
                reason=reason,
            )
            return Outcome.success(label="skip", outputs={"reason": reason})

        return Outcome.success(outputs={"pr_external_id": payload.get("pr_external_id")})


def _decide_skip(payload: dict[str, Any]) -> str | None:
    """First-match-wins admission. Returns a skip reason string or None for go."""
    if payload.get("is_draft"):
        return "draft"
    if payload.get("is_fork"):
        return "fork"
    labels = {str(label).lower() for label in (payload.get("labels") or [])}
    forced = labels & {label.lower() for label in SKIP_LABELS}
    if forced:
        return f"label:{sorted(forced)[0]}"
    author = (payload.get("author_login") or "").lower()
    if author.endswith("[bot]") or author.endswith("-bot"):
        return "bot_author"
    return None


class SecretsScan:
    """Pre-flight secrets gate. Fetches the PR diff via the VCS plugin and
    runs `secrets_detection.detect_secrets`. If any known secret pattern is
    matched in `+`-prefixed (added) lines:

    - Returns `Outcome.success(label="skip", outputs={"reason": "secrets_detected", "rule_id": <id>})`
      so the workflow's `skip` transition terminates the run (per
      `pr_review_v1`).
    - Posts a `secrets_warning_review` to the PR via the registered VCS
      plugin so the human sees yaaos's refusal in-band.

    Matches the legacy `_run_review_job_inner` behavior (slice 46) — the
    legacy path is the existing production owner of secrets detection;
    the workflow needs the same gate or it's a regression.

    No `pr_id`? The workflow can't fetch the diff yet; treat as no-op
    success (CheckShouldReview already handled the ticket-payload skip
    signals upstream). Same for VCS errors: best-effort, log + advance.
    """

    kind = "SecretsScan"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs

        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")
        try:
            ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        except Exception as exc:
            log.exception(
                "secrets_scan.context_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")
        if ticket_ctx is None or ticket_ctx.pr_id is None:
            log.info(
                "secrets_scan.no_pr_link",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.success(outputs={"rule_id": None})

        # Deferred imports — keep the commands module-import cheap.
        from app.domain.reviewer.secrets_detection import (  # noqa: PLC0415
            detect_secrets,
            secrets_warning_review,
        )
        from app.domain.vcs import get_plugin as get_vcs_plugin  # noqa: PLC0415

        try:
            vcs_plugin = get_vcs_plugin(ticket_ctx.plugin_id)
            pr_external_id = str(ticket_ctx.payload.get("pr_external_id") or "")
            if not pr_external_id:
                return Outcome.success(outputs={"rule_id": None})
            diff = await vcs_plugin.fetch_diff(pr_external_id)
        except Exception as exc:
            # Best-effort — diff fetch shouldn't block reviews. Logged for
            # ops visibility but the workflow advances to ProvisionWorkspace.
            log.warning(
                "secrets_scan.diff_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return Outcome.success(outputs={"rule_id": None})

        rule_id = detect_secrets(diff)
        if rule_id is None:
            return Outcome.success(outputs={"rule_id": None})

        # Best-effort post the warning Review; if it fails we still skip.
        try:
            await vcs_plugin.post_review(pr_external_id, secrets_warning_review(rule_id))
        except Exception:
            log.exception(
                "secrets_scan.post_warning_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                rule_id=rule_id,
            )

        log.info(
            "secrets_scan.detected",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
            rule_id=rule_id,
        )
        return Outcome.success(
            label="skip",
            outputs={"reason": "secrets_detected", "rule_id": rule_id},
        )


class PostFindings(_LocalReviewCommand):
    """Persist coding-agent findings through the admission pipeline.

    Inputs:
    - `draft_findings`: list of `FindingDraft`-shaped dicts (as the
      upstream `CodeReview` Workspace step will emit them).
    - `workspace_id`: the workspace the drafts were produced against.
      Needed because `findingdrafts_to_raw` reads anchored file content
      from the workspace to build stable fingerprints.

    Flow: deserialize FindingDrafts → read referenced files from the
    workspace → `findingdrafts_to_raw` → `admit_raw_findings`. Outputs
    `admitted_count` + `dropped_count` so audit consumers can see the
    admission ratio.

    Defensive: empty/missing `draft_findings` → success-no-op (a stubbed
    upstream review or a successful review with nothing to report). Missing
    workspace or ticket context → failure. Bad FindingDraft schema in any
    item → failure (caller should not have produced it).

    NOT included yet: posting admitted findings to GitHub via
    `vcs.post_review`. That's a separate slice that wires the vcs plugin
    lookup + thread bookkeeping; admitted findings persist in the aggregate
    today and become visible to future review runs.
    """

    kind = "PostFindings"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        drafts_raw = inputs.get("draft_findings") or []
        if not drafts_raw:
            return Outcome.success(outputs={"admitted_count": 0, "dropped_count": 0})

        ws_id_raw = inputs.get("workspace_id")
        if not ws_id_raw:
            return Outcome.failure(reason="missing workspace_id input")
        try:
            ws_id = UUID(str(ws_id_raw))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid workspace_id: {ws_id_raw!r}")

        workspace = await get_workspace(ws_id)
        if workspace is None:
            return Outcome.failure(reason=f"workspace {ws_id} not resolvable")

        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")

        ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        if ticket_ctx is None or ticket_ctx.pr_id is None:
            log.info(
                "post_findings.no_pr_link",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.success(outputs={"admitted_count": 0, "dropped_count": 0})

        # Deferred imports — keep the commands module-import cheap for the
        # engine registration path.
        from app.domain.coding_agent import FindingDraft  # noqa: PLC0415
        from app.domain.reviewer.admission import (  # noqa: PLC0415
            admit_raw_findings,
            findingdrafts_to_raw,
        )

        # 1. Deserialize FindingDrafts.
        try:
            drafts = [FindingDraft.model_validate(d) for d in drafts_raw]
        except Exception as exc:
            return Outcome.failure(reason=f"invalid FindingDraft payload: {exc}")

        # 2. Pre-fetch file contents for each referenced anchor file. Using a
        # dict.get matches the sync read_file signature `findingdrafts_to_raw`
        # expects; the workspace's own read is async.
        file_contents: dict[str, list[str] | None] = {}
        for draft in drafts:
            path = draft.anchor.file_path
            if path in file_contents:
                continue
            text = await workspace.read_text(path)
            file_contents[path] = text.splitlines() if text else None

        commit_sha = str(ticket_ctx.payload.get("head_sha") or "")
        raw = findingdrafts_to_raw(
            drafts,
            commit_sha=commit_sha,
            read_file=file_contents.get,
        )

        # 3. Persist via admission. The wrapper opens a new Review row so
        # the findings FK to `reviews` is satisfied; the workflow_execution_id
        # is stamped onto the trace context for cross-system correlation.
        async with db_session() as s:
            result = await admit_raw_findings(
                pr_id=ticket_ctx.pr_id,
                org_id=ticket_ctx.org_id,
                raw=raw,
                commit_sha=commit_sha,
                session=s,
            )
            await s.commit()

        # 4. Post admitted findings to the VCS plugin (GitHub). Only fires
        # when there's something to post; the helper looks up the PR row
        # for the external id. VCS failures are surfaced as Outcome.failure
        # so the workflow can decide retry/cleanup — the admitted findings
        # already persisted in step 3 so they're not lost.
        posted = False
        if result.admitted:
            from app.domain.pull_requests import PullRequestNotFoundError  # noqa: PLC0415
            from app.domain.pull_requests import get as get_pull_request  # noqa: PLC0415
            from app.domain.reviewer.admission import (  # noqa: PLC0415
                post_admitted_findings_to_vcs,
            )

            try:
                pr_row = await get_pull_request(ticket_ctx.pr_id, org_id=ticket_ctx.org_id)
            except PullRequestNotFoundError:
                pr_row = None

            if pr_row is None:
                log.warning(
                    "post_findings.no_pr_row",
                    workflow_execution_id=ctx.workflow_execution_id,
                    pr_id=str(ticket_ctx.pr_id),
                )
            else:
                try:
                    async with db_session() as s:
                        await post_admitted_findings_to_vcs(
                            pr_id=ticket_ctx.pr_id,
                            org_id=ticket_ctx.org_id,
                            pr_external_id=pr_row.external_id,
                            vcs_plugin_id=pr_row.plugin_id,
                            admitted=result.admitted,
                            raw=raw,
                            summary_body=None,
                            session=s,
                        )
                        await s.commit()
                    posted = True
                except Exception as exc:
                    log.exception(
                        "post_findings.vcs_post_failed",
                        workflow_execution_id=ctx.workflow_execution_id,
                        pr_id=str(ticket_ctx.pr_id),
                    )
                    return Outcome.failure(
                        reason=f"vcs.post_review failed: {type(exc).__name__}: {exc}",
                        outputs={
                            "admitted_count": len(result.admitted),
                            "dropped_count": len(result.drops),
                            "posted": False,
                        },
                    )

        log.info(
            "post_findings.done",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
            drafts_in=len(drafts_raw),
            drafts_after_read=len(raw),
            admitted=len(result.admitted),
            dropped=len(result.drops),
            posted=posted,
        )
        return Outcome.success(
            outputs={
                "admitted_count": len(result.admitted),
                "dropped_count": len(result.drops),
                "posted": posted,
            }
        )


class ResolveFinding(_LocalReviewCommand):
    """Apply a verify-fix verdict to a single finding. Receives `verdict`
    from inputs (sourced from the prior `VerifyFix` Workspace step via
    `$verify.verdict`). The verdict shape mirrors `coding_agent.VerifyFixResult`:

        {"finding_id": "<uuid>", "still_present": <bool>, "confidence": <float>}

    Calls `aggregate.record_fix_verification(...)` which transitions the
    finding to `RESOLVED_CONFIRMED` iff `still_present=False` AND
    `confidence ≥ threshold` (default 0.80). Lower-confidence verdicts and
    `still_present=True` are no-ops — the finding stays open and the
    workflow ends cleanly.

    Defensive: empty/missing verdict → success-no-op. Missing pr_id link →
    success-no-op. Unknown finding_id → skipped, not failed.

    Outputs:
    - `transitioned_to`: the new state (string) if a transition fired, else None
    """

    kind = "ResolveFinding"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        verdict = inputs.get("verdict") or {}
        if not isinstance(verdict, dict) or not verdict:
            return Outcome.success(outputs={"transitioned_to": None})

        finding_id_raw = verdict.get("finding_id")
        if not finding_id_raw:
            return Outcome.success(outputs={"transitioned_to": None})
        try:
            finding_id = UUID(str(finding_id_raw))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid finding_id: {finding_id_raw!r}")

        still_present = bool(verdict.get("still_present", True))
        try:
            confidence = float(verdict.get("confidence", 0.0))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid confidence: {verdict.get('confidence')!r}")

        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")

        try:
            ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        except Exception as exc:
            log.exception(
                "resolve_finding.context_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        if ticket_ctx is None or ticket_ctx.pr_id is None:
            log.info(
                "resolve_finding.no_pr_link",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.success(outputs={"transitioned_to": None})

        from app.domain.reviewer.repository import (  # noqa: PLC0415
            SqlAlchemyAggregateRepository,
        )

        async with db_session() as s:
            repo = SqlAlchemyAggregateRepository(s)
            aggregate = await repo.load(pr_id=ticket_ctx.pr_id, org_id=ticket_ctx.org_id)
            known_ids = {f.id for f in aggregate.findings}
            if finding_id not in known_ids:
                log.info(
                    "resolve_finding.unknown_finding",
                    workflow_execution_id=ctx.workflow_execution_id,
                    finding_id=str(finding_id),
                )
                return Outcome.success(outputs={"transitioned_to": None})

            new_state = aggregate.record_fix_verification(
                finding_id=finding_id,
                still_present=still_present,
                confidence=confidence,
            )
            await repo.save(aggregate)
            await s.commit()

        log.info(
            "resolve_finding.done",
            workflow_execution_id=ctx.workflow_execution_id,
            finding_id=str(finding_id),
            still_present=still_present,
            confidence=confidence,
            transitioned_to=(new_state.value if new_state is not None else None),
        )
        return Outcome.success(
            outputs={"transitioned_to": new_state.value if new_state is not None else None}
        )


class ArchiveStaleFindings(_LocalReviewCommand):
    """Mark a list of findings as `STALE` in the reviewer aggregate. Receives
    `stale_finding_ids: list[str]` from inputs — typically sourced from the
    prior `StaleCheck` Workspace step via `$check.stale_finding_ids`.

    Idempotent and defensive:
    - Empty / missing input → success-no-op.
    - Ticket with no `pr_id` (intake created the ticket before PR
      materialization) → success-no-op. Nothing to archive.
    - Individual `finding_id` not present in the aggregate → skipped, not a
      failure. The aggregate enumerates findings owned by this PR; ids from
      a stale upstream payload (e.g. finding was hard-deleted) shouldn't
      sink the whole step.

    Outputs `archived_count` so downstream steps or audits can read how
    many state transitions actually fired.
    """

    kind = "ArchiveStaleFindings"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        stale_ids_raw = inputs.get("stale_finding_ids") or []
        if not stale_ids_raw:
            return Outcome.success(outputs={"archived_count": 0})

        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")

        try:
            ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        except Exception as exc:
            log.exception(
                "archive_stale_findings.context_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        if ticket_ctx is None or ticket_ctx.pr_id is None:
            log.info(
                "archive_stale_findings.no_pr_link",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.success(outputs={"archived_count": 0})

        # Defer the heavy SqlAlchemyAggregateRepository import — keeps the
        # commands module-import cheap for the engine registration path.
        from app.domain.reviewer.repository import (  # noqa: PLC0415
            SqlAlchemyAggregateRepository,
        )

        archived = 0
        skipped = 0
        async with db_session() as s:
            repo = SqlAlchemyAggregateRepository(s)
            aggregate = await repo.load(pr_id=ticket_ctx.pr_id, org_id=ticket_ctx.org_id)
            known_ids = {f.id for f in aggregate.findings}
            for raw_id in stale_ids_raw:
                try:
                    fid = UUID(str(raw_id))
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                if fid not in known_ids:
                    skipped += 1
                    continue
                # confidence=1.0 because StaleCheck has already decided;
                # ArchiveStaleFindings is the durable persistence step.
                result = aggregate.record_stale_detection(finding_id=fid, still_applies=False, confidence=1.0)
                if result is not None:
                    archived += 1
            await repo.save(aggregate)
            await s.commit()

        log.info(
            "archive_stale_findings.done",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
            archived=archived,
            skipped=skipped,
        )
        return Outcome.success(outputs={"archived_count": archived, "skipped_count": skipped})


class PostReply(_LocalReviewCommand):
    """Append a yaaos-authored reply to a finding's comment thread.

    Inputs:
    - `reply_body`: text of the reply (from the prior `AnswerQuestion`
      Workspace step).
    - `finding_id`: which finding's thread to reply on (from
      `$ticket.finding_id`).

    Loads the reviewer aggregate by `pr_id`, finds the thread for
    `finding_id`, appends a `CommentMessage` with `author_kind="yaaos"`.
    The external_comment_id is set to a `local-<uuid>` placeholder — the
    GitHub-side post (vcs.post_comment_reply) is a follow-on slice; today
    the reply persists locally but doesn't appear on GitHub yet.

    Defensive: empty/missing inputs → success-no-op (workflow drain).
    Missing pr_id, unknown finding, or no existing thread → success-no-op
    with a log line (the reply has nothing to attach to). Real errors
    (provider missing) return failure.
    """

    kind = "PostReply"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        reply_body = inputs.get("reply_body")
        finding_id_raw = inputs.get("finding_id")
        if not reply_body or not finding_id_raw:
            return Outcome.success(outputs={"posted": False, "reason": "empty_input"})

        try:
            finding_id = UUID(str(finding_id_raw))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid finding_id: {finding_id_raw!r}")

        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")

        ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        if ticket_ctx is None or ticket_ctx.pr_id is None:
            log.info(
                "post_reply.no_pr_link",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.success(outputs={"posted": False, "reason": "no_pr_link"})

        # Deferred import — heavy module.
        from app.domain.reviewer.repository import (  # noqa: PLC0415
            SqlAlchemyAggregateRepository,
        )

        async with db_session() as s:
            repo = SqlAlchemyAggregateRepository(s)
            aggregate = await repo.load(pr_id=ticket_ctx.pr_id, org_id=ticket_ctx.org_id)
            known_ids = {f.id for f in aggregate.findings}
            if finding_id not in known_ids:
                log.info(
                    "post_reply.unknown_finding",
                    workflow_execution_id=ctx.workflow_execution_id,
                    finding_id=str(finding_id),
                )
                return Outcome.success(outputs={"posted": False, "reason": "unknown_finding"})

            thread = aggregate.thread_for_finding(finding_id)
            if thread is None:
                log.info(
                    "post_reply.no_thread",
                    workflow_execution_id=ctx.workflow_execution_id,
                    finding_id=str(finding_id),
                )
                return Outcome.success(outputs={"posted": False, "reason": "no_thread"})

            # Find the parent yaaos comment on the thread — the first message
            # by author_kind="yaaos" carries the external_comment_id we reply
            # under. If none exists (thread opened but never posted), fall
            # through to local-only persist.
            parent_external_id = None
            for msg in aggregate.messages:
                if msg.thread_id == thread.id and msg.author_kind == "yaaos":
                    parent_external_id = msg.external_comment_id
                    break

            # Look up the PR for the external id. Required to call
            # vcs.post_comment_reply.
            from app.domain.pull_requests import PullRequestNotFoundError  # noqa: PLC0415
            from app.domain.pull_requests import get as get_pull_request  # noqa: PLC0415

            try:
                pr_row = await get_pull_request(ticket_ctx.pr_id, org_id=ticket_ctx.org_id)
            except PullRequestNotFoundError:
                pr_row = None

            external_comment_id = f"local-reply-{uuid4()}"  # fallback
            if parent_external_id is None or pr_row is None or parent_external_id.startswith("local-"):
                # No real parent yet (e.g. PostFindings never posted to GitHub
                # for this finding) — persist locally with placeholder. Same
                # behavior as before slice 32.
                log.info(
                    "post_reply.local_only",
                    workflow_execution_id=ctx.workflow_execution_id,
                    finding_id=str(finding_id),
                    reason="no_real_parent" if parent_external_id else "no_pr_row",
                )
            else:
                # Real parent + real PR row → post to GitHub.
                try:
                    from app.domain.vcs import get_plugin as get_vcs_plugin  # noqa: PLC0415

                    vcs_plugin = get_vcs_plugin(pr_row.plugin_id)
                    external_comment_id = await vcs_plugin.post_comment_reply(
                        pr_row.external_id, parent_external_id, str(reply_body)
                    )
                except Exception as exc:
                    log.exception(
                        "post_reply.vcs_post_failed",
                        workflow_execution_id=ctx.workflow_execution_id,
                        finding_id=str(finding_id),
                    )
                    return Outcome.failure(
                        reason=f"vcs.post_comment_reply failed: {type(exc).__name__}: {exc}",
                    )

            aggregate.append_message(
                thread_id=thread.id,
                author_kind="yaaos",
                author_external_id="yaaos",
                external_comment_id=external_comment_id,
                in_reply_to_external_id=parent_external_id,
                body=str(reply_body),
            )
            await repo.save(aggregate)
            await s.commit()
            placeholder_external_id = external_comment_id  # for the existing return-shape

        log.info(
            "post_reply.persisted",
            workflow_execution_id=ctx.workflow_execution_id,
            finding_id=str(finding_id),
            thread_id=str(thread.id),
        )
        return Outcome.success(
            outputs={
                "posted": True,
                "thread_id": str(thread.id),
                "external_comment_id": placeholder_external_id,
            }
        )


ALL_WORKSPACE_COMMANDS: tuple[_WorkspaceReviewCommand, ...] = (
    CodeReview(),
    IncrementalReview(),
    VerifyFix(),
    StaleCheck(),
    AnswerQuestion(),
)

ALL_LOCAL_COMMANDS: tuple[object, ...] = (
    CheckShouldReview(),
    SecretsScan(),
    PostFindings(),
    ResolveFinding(),
    ArchiveStaleFindings(),
    PostReply(),
)


__all__ = [
    "ALL_LOCAL_COMMANDS",
    "ALL_WORKSPACE_COMMANDS",
    "AnswerQuestion",
    "ArchiveStaleFindings",
    "CheckShouldReview",
    "CodeReview",
    "IncrementalReview",
    "PostFindings",
    "PostReply",
    "ResolveFinding",
    "SecretsScan",
    "StaleCheck",
    "VerifyFix",
]
