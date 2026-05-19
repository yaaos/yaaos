"""HTTP routes for tickets."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.audit_log import list_for_entity
from app.core.auth import public_route
from app.core.webserver import RouteSpec, register_routes
from app.domain.tickets.service import (
    Ticket,
    TicketFilter,
    TicketNotFoundError,
    get,
    list_tickets,
)

M01_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

# M02 default-deny: every legacy /api/* route declares security. M01-era
# tickets endpoints aren't org-scoped via X-Org-Slug yet — they hard-code
# `M01_ORG_ID`. Mark them as explicitly public for now; the M03+ migration
# to per-org access will swap this for `require(Action.X)`.
router = APIRouter(dependencies=[Depends(public_route)])


@router.get("")
async def list_(
    repo_external_id: list[str] | None = Query(default=None),
    status: list[str] | None = Query(default=None),
    limit: int = 50,
) -> list[Ticket]:
    filter_ = TicketFilter(repo_external_ids=repo_external_id, statuses=status)  # type: ignore[arg-type]
    return await list_tickets(filter_, org_id=M01_ORG_ID, limit=limit)


@router.get("/{ticket_id}")
async def detail(ticket_id: UUID) -> Ticket:
    try:
        return await get(ticket_id, org_id=M01_ORG_ID)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")


@router.get("/{ticket_id}/audit")
async def audit(ticket_id: UUID, limit: int = 200) -> list[dict[str, Any]]:
    """Aggregated timeline: ticket + its PR + every review_job for that PR
    + every finding raised against that PR (so reply-flow events like
    `finding_acknowledged` surface in the ticket-level audit feed)."""
    from app.domain import reviewer as reviewer_mod  # noqa: PLC0415

    try:
        ticket = await get(ticket_id, org_id=M01_ORG_ID)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    entries = await list_for_entity("ticket", ticket_id, org_id=M01_ORG_ID, limit=limit)
    if ticket.pr_id is not None:
        entries.extend(await list_for_entity("pull_request", ticket.pr_id, org_id=M01_ORG_ID, limit=limit))
        jobs = await reviewer_mod.list_review_jobs_for_pr(ticket.pr_id, org_id=M01_ORG_ID)
        for j in jobs:
            entries.extend(await list_for_entity("review_job", j.id, org_id=M01_ORG_ID, limit=limit))
        findings = await reviewer_mod.list_findings_for_pr(
            ticket.pr_id, org_id=M01_ORG_ID, include_terminal=True
        )
        for f in findings:
            entries.extend(await list_for_entity("finding", f.id, org_id=M01_ORG_ID, limit=limit))
    entries.sort(key=lambda e: e.created_at, reverse=True)
    return [e.model_dump(mode="json") for e in entries[:limit]]


register_routes(RouteSpec(module_name="tickets", router=router))
