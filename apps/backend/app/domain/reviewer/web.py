"""HTTP routes for review history and findings reads.

Cancel goes through the workflow engine. Finding reads return the canonical
schema: severity ∈ {blocker, should_fix, nit}, confidence ∈ {verified,
plausible, speculative}, category, rationale.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import Action, org_id_var
from app.core.database import session
from app.core.sessions import require
from app.core.webserver import RouteSpec, register_routes
from app.domain import tickets
from app.domain.reviewer.models import ReviewRow
from app.domain.reviewer.service import (
    list_findings_for_pr,
)

router = APIRouter()


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


def _org() -> UUID:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    return org_id


@router.post("/cancel", dependencies=[Depends(require(Action.REVIEWER_WRITE))])
async def cancel_jobs(ticket_id: UUID) -> dict[str, int]:
    """Cancel any non-terminal workflow_executions for this ticket."""
    org_id = _org()
    try:
        await tickets.get(ticket_id, org_id=org_id)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")

    from app.domain.reviewer import cancel_workflows_for_ticket  # noqa: PLC0415

    cancelled = await cancel_workflows_for_ticket(ticket_id)
    return {"cancelled_count": cancelled}


@router.get("/metrics", dependencies=[Depends(require(Action.REVIEWER_READ))])
async def metrics() -> dict[str, Any]:
    """Aggregate review counters."""
    from app.domain.reviewer.workflow_review_view import (  # noqa: PLC0415
        workflow_metrics_summary,
    )

    workflow = await workflow_metrics_summary(org_id=_org())
    by_status: dict[str, int] = dict(workflow.get("review_jobs_by_status") or {})
    posted = workflow.get("total_reviews_posted") or 0
    failed = workflow.get("failure_count") or 0
    return {
        "review_jobs_by_status": by_status,
        "total_reviews_posted": posted,
        "failure_count": failed,
        "failure_rate": (failed / (posted + failed)) if (posted + failed) > 0 else 0.0,
    }


@router.get(
    "/findings/by-ticket/{ticket_id}",
    dependencies=[Depends(require(Action.REVIEWER_READ))],
)
async def findings_by_ticket(ticket_id: UUID, include_terminal: bool = False) -> list[dict[str, Any]]:
    """List findings for the ticket's PR in the canonical schema."""
    org_id = _org()
    try:
        t = await tickets.get(ticket_id, org_id=org_id)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t.pr_id is None:
        return []
    views = await list_findings_for_pr(t.pr_id, org_id=org_id, include_terminal=include_terminal)
    return [
        {
            "id": str(f.id),
            "finding_display_id": f.finding_display_id,
            "category": f.category,
            "severity": f.severity,
            "confidence": f.confidence,
            "rationale": f.rationale,
            "rule_violated": f.rule_violated,
            "rule_source": f.rule_source,
            "suggested_fix": f.suggested_fix,
            "file": f.file,
            "line": f.line,
            "review_id": str(f.review_id),
        }
        for f in views
    ]


@router.get(
    "/reviews/by-ticket/{ticket_id}",
    dependencies=[Depends(require(Action.REVIEWER_READ))],
)
async def reviews_by_ticket(ticket_id: UUID) -> list[dict[str, Any]]:
    """Per-review timeline metadata."""
    from sqlalchemy import desc, select  # noqa: PLC0415

    org_id = _org()
    try:
        t = await tickets.get(ticket_id, org_id=org_id)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t.pr_id is None:
        return []
    async with session() as s:
        rows = (
            (
                await s.execute(
                    select(ReviewRow)
                    .where(ReviewRow.pr_id == t.pr_id, ReviewRow.org_id == org_id)
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
            "commit_sha_at_start": r.commit_sha_at_start,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


async def _start_orphan_sweep() -> None:
    from app.core.observability import spawn  # noqa: PLC0415
    from app.domain.reviewer.orphan_sweep import run_sweep_loop  # noqa: PLC0415

    spawn("reviewer.orphan_sweep", run_sweep_loop())


register_routes(
    RouteSpec(
        module_name="reviewer",
        router=router,
        on_startup=[_start_orphan_sweep],
    )
)
