"""HTTP routes for review-job + durable-findings operations."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.database import session
from app.core.primitives import Actor
from app.core.webserver import RouteSpec, register_routes
from app.domain import tickets
from app.domain.reviewer.queue import (
    ReviewJob,
    cancel_pending,
    list_review_jobs_for_pr,
    metrics_summary,
    schedule_review,
    startup_recovery,
)
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.service import (
    all_conversations_view,
    list_findings_view,
)

M01_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

router = APIRouter()


class RereviewRequest(BaseModel):
    ticket_id: UUID


@router.post("/rereview")
async def rereview_ticket(req: RereviewRequest) -> dict[str, Any]:
    try:
        await tickets.get(req.ticket_id, org_id=M01_ORG_ID)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    job_id = await schedule_review(
        ticket_id=req.ticket_id,
        trigger_reason="ui_button",
        actor=Actor.system(),
        org_id=M01_ORG_ID,
    )
    return {
        "scheduled_count": 1 if job_id else 0,
        "review_job_id": str(job_id) if job_id else None,
    }


@router.post("/cancel")
async def cancel_jobs(ticket_id: UUID) -> dict[str, int]:
    """Cancel queued/running review jobs for a ticket."""
    try:
        await tickets.get(ticket_id, org_id=M01_ORG_ID)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    n = await cancel_pending(ticket_id, actor=Actor.system(), org_id=M01_ORG_ID, reason="ui_cancel")
    return {"cancelled_count": n}


@router.get("/jobs/by-ticket/{ticket_id}")
async def jobs_by_ticket(ticket_id: UUID) -> list[ReviewJob]:
    try:
        t = await tickets.get(ticket_id, org_id=M01_ORG_ID)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t.pr_id is None:
        return []
    return await list_review_jobs_for_pr(t.pr_id, org_id=M01_ORG_ID)


@router.get("/metrics")
async def metrics() -> dict[str, Any]:
    return await metrics_summary(org_id=M01_ORG_ID)


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


register_routes(
    RouteSpec(
        module_name="reviewer",
        router=router,
        on_startup=[startup_recovery],
    )
)
