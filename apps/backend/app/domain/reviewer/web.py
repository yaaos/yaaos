"""HTTP routes for review-job + durable-findings operations."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import public_route
from app.core.database import session
from app.core.webserver import RouteSpec, register_routes
from app.domain import tickets
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.review_job import ReviewJob
from app.domain.reviewer.service import (
    all_conversations_view,
    list_findings_view,
)

M01_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

# M02 default-deny: legacy reviewer endpoints declare `public_route` so the
# middleware's post-response guard recognizes the declaration. M03+ migration
# to per-org access swaps this for `require(Action.X)`.
router = APIRouter(dependencies=[Depends(public_route)])


class RereviewRequest(BaseModel):
    ticket_id: UUID


@router.post("/rereview")
async def rereview_ticket(req: RereviewRequest) -> dict[str, Any]:
    """Re-review a ticket — drives `pr_review_v1` via the M05 workflow engine.

    Replaces the legacy `schedule_review` / `review_jobs` flow. The SPA's
    only contract with this endpoint is the `scheduled_count` field; the
    response now carries `workflow_execution_id` instead of `review_job_id`
    so the caller can poll workflow state if desired.
    """
    try:
        await tickets.get(req.ticket_id, org_id=M01_ORG_ID)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")

    # Read ticket context via the workflow-context provider (registered at
    # domain/reviewer bootstrap). Routes the same path intake uses, so the
    # workflow engine receives a payload-derived `$ticket.*` view + the
    # right org id without re-fetching here.
    from app.core.workflow import get_engine  # noqa: PLC0415
    from app.core.workspace import get_workflow_context_provider  # noqa: PLC0415

    provider = get_workflow_context_provider()
    if provider is None:
        raise HTTPException(
            status_code=500,
            detail="workflow context provider not registered",
        )
    ctx = await provider.get_workspace_ticket_context(req.ticket_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="ticket not found")

    async with session() as s:
        workflow_execution_id = await get_engine().start(
            workflow_name="pr_review_v1",
            ticket_id=str(req.ticket_id),
            workspace_provider="in_memory",
            ticket_payload=dict(ctx.payload),
            session=s,
        )
        await s.commit()

    return {
        "scheduled_count": 1,
        "workflow_execution_id": workflow_execution_id,
    }


@router.post("/cancel")
async def cancel_jobs(ticket_id: UUID) -> dict[str, int]:
    """Cancel any non-terminal workflow_executions for this ticket.

    The legacy `review_jobs` cancel path was retired with the queue.py
    dismantle (slice 60); now `workflow.request_cancel` is the single
    source of truth. The engine transitions the workflow to `cancelled`
    at its next step boundary.
    """
    try:
        await tickets.get(ticket_id, org_id=M01_ORG_ID)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")

    from app.domain.reviewer import cancel_workflows_for_ticket  # noqa: PLC0415

    cancelled = await cancel_workflows_for_ticket(ticket_id)
    return {"cancelled_count": cancelled}


@router.get("/jobs/by-ticket/{ticket_id}")
async def jobs_by_ticket(ticket_id: UUID) -> list[ReviewJob]:
    """Per-ticket review history.

    Reads from `workflow_executions` for this ticket, projected into the
    `ReviewJob` shape via `workflow_review_view`. Newest first. The
    legacy `review_jobs` merge was dropped with the queue.py dismantle
    (slice 60) — that table is no longer written by any code path.
    """
    from app.domain.reviewer.workflow_review_view import (  # noqa: PLC0415
        list_review_jobs_for_ticket as list_workflow_jobs,
    )

    try:
        t = await tickets.get(ticket_id, org_id=M01_ORG_ID)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")

    # Workflow projection needs a PR id to populate `pr_id`. Use the
    # ticket's pr_id if present; otherwise zero-UUID (SPA tolerates).
    pr_id_for_projection = t.pr_id or UUID(int=0)
    rows = await list_workflow_jobs(ticket_id, pr_id=pr_id_for_projection, org_id=M01_ORG_ID)
    rows.sort(key=lambda j: j.scheduled_at, reverse=True)
    return rows


@router.get("/metrics")
async def metrics() -> dict[str, Any]:
    """Aggregate review counters, sourced from `workflow_executions` via
    the workflow_review_view projection. The legacy `review_jobs` source
    was dropped with the queue.py dismantle (slice 60)."""
    from app.domain.reviewer.workflow_review_view import (  # noqa: PLC0415
        workflow_metrics_summary,
    )

    workflow = await workflow_metrics_summary(org_id=M01_ORG_ID)
    by_status: dict[str, int] = dict(workflow.get("review_jobs_by_status") or {})
    posted = workflow.get("total_reviews_posted") or 0
    failed = workflow.get("failure_count") or 0
    return {
        "review_jobs_by_status": by_status,
        "total_reviews_posted": posted,
        "failure_count": failed,
        "failure_rate": (failed / (posted + failed)) if (posted + failed) > 0 else 0.0,
    }


@router.get("/findings/by-ticket/{ticket_id}")
async def findings_by_ticket(ticket_id: UUID, include_terminal: bool = False) -> list[dict[str, Any]]:
    """List open + acknowledged findings for the ticket's PR.

    Set `include_terminal=true` to also return resolved + stale findings.
    """
    try:
        t = await tickets.get(ticket_id, org_id=M01_ORG_ID)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t.pr_id is None:
        return []
    async with session() as s:
        repo = SqlAlchemyAggregateRepository(s)
        aggregate = await repo.load(pr_id=t.pr_id, org_id=M01_ORG_ID)
    return [
        {
            "id": str(f.id),
            "state": f.state.value,
            "severity": f.severity,
            "rule_id": f.rule_id,
            "title": f.title,
            "body": f.body,
            "rationale": f.rationale,
            "confidence": f.confidence,
            "first_seen_review_id": str(f.first_seen_review_id),
            "last_observed_review_id": str(f.last_observed_review_id),
            "file_path": f.file_path,
            "line_start": f.line_start,
            "line_end": f.line_end,
        }
        for f in list_findings_view(aggregate, include_terminal=include_terminal)
    ]


@router.get("/conversations/by-ticket/{ticket_id}")
async def conversations_by_ticket(ticket_id: UUID) -> list[dict[str, Any]]:
    """All-Conversations cross-cut (plan §9.3) for the ticket's PR."""
    try:
        t = await tickets.get(ticket_id, org_id=M01_ORG_ID)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t.pr_id is None:
        return []
    async with session() as s:
        repo = SqlAlchemyAggregateRepository(s)
        aggregate = await repo.load(pr_id=t.pr_id, org_id=M01_ORG_ID)
    return [
        {
            "finding_id": str(c.finding_id),
            "state": c.state.value,
            "severity": c.severity,
            "title": c.title,
            "first_seen_review_id": str(c.first_seen_review_id),
            "last_message_preview": c.last_message_preview,
            "reply_count": c.reply_count,
        }
        for c in all_conversations_view(aggregate)
    ]


@router.get("/reviews/by-ticket/{ticket_id}")
async def reviews_by_ticket(ticket_id: UUID) -> list[dict[str, Any]]:
    """Per-review timeline metadata (plan §9.2).

    Returns one row per Review for the ticket's PR, newest first. Each row
    carries `sequence_number`, `trigger_reason`, `scope_kind`/`scope_prev_sha`,
    `commit_sha_at_start`, status, timestamps, model/tokens — everything the
    UI needs to render the collapsible per-review section header.
    """
    from sqlalchemy import desc, select  # noqa: PLC0415

    from app.domain.reviewer.models import ReviewRow  # noqa: PLC0415

    try:
        t = await tickets.get(ticket_id, org_id=M01_ORG_ID)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t.pr_id is None:
        return []
    async with session() as s:
        rows = (
            (
                await s.execute(
                    select(ReviewRow)
                    .where(ReviewRow.pr_id == t.pr_id, ReviewRow.org_id == M01_ORG_ID)
                    .order_by(desc(ReviewRow.sequence_number))
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": str(r.id),
            "sequence_number": r.sequence_number,
            "trigger_reason": r.trigger_reason,
            "scope_kind": r.scope_kind,
            "scope_prev_sha": r.scope_prev_sha,
            "commit_sha_at_start": r.commit_sha_at_start,
            "status": r.status,
            "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "model": r.model,
            "effort": r.effort,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "duration_s": r.duration_s,
        }
        for r in rows
    ]


@router.get("/threads/by-finding/{finding_id}")
async def thread_by_finding(finding_id: UUID) -> dict[str, Any]:
    """Thread messages + ack banner for one finding (plan §9.4)."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.domain.reviewer.models import (  # noqa: PLC0415
        AcknowledgmentDecisionRow,
        CommentMessageRow,
        CommentThreadRow,
        FindingRow,
    )

    async with session() as s:
        finding = (
            await s.execute(select(FindingRow).where(FindingRow.id == finding_id))
        ).scalar_one_or_none()
        if finding is None:
            raise HTTPException(status_code=404, detail="finding not found")
        if finding.org_id != M01_ORG_ID:
            raise HTTPException(status_code=404, detail="finding not found")
        thread = (
            await s.execute(select(CommentThreadRow).where(CommentThreadRow.finding_id == finding_id))
        ).scalar_one_or_none()
        messages: list[Any] = []
        ack: dict[str, Any] | None = None
        if thread is not None:
            messages = list(
                (
                    await s.execute(
                        select(CommentMessageRow)
                        .where(CommentMessageRow.thread_id == thread.id)
                        .order_by(CommentMessageRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
        ack_row = (
            await s.execute(
                select(AcknowledgmentDecisionRow)
                .where(AcknowledgmentDecisionRow.finding_id == finding_id)
                .order_by(AcknowledgmentDecisionRow.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if ack_row is not None:
            ack = {
                "kind": ack_row.kind,
                "rationale": ack_row.rationale,
                "made_by_external_id": ack_row.made_by_external_id,
                "created_at": ack_row.created_at.isoformat() if ack_row.created_at else None,
            }
    return {
        "finding_id": str(finding_id),
        "state": finding.state,
        "title": finding.title,
        "thread_id": str(thread.id) if thread else None,
        "external_thread_id": thread.external_thread_id if thread else None,
        "acknowledgment": ack,
        "messages": [
            {
                "id": str(m.id),
                "author_kind": m.author_kind,
                "author_external_id": m.author_external_id,
                "external_comment_id": m.external_comment_id,
                "body": m.body,
                "classified_intent": m.classified_intent,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
    }


register_routes(
    RouteSpec(
        module_name="reviewer",
        router=router,
    )
)
