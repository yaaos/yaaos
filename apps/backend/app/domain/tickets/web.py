"""HTTP routes for tickets.

| Method | Path                                                                       | Action          |
|--------|----------------------------------------------------------------------------|-----------------|
| GET    | `/api/tickets`                                                             | `TICKETS_READ`  |
| GET    | `/api/tickets/{ticket_id}`                                                 | `TICKETS_READ`  |
| GET    | `/api/tickets/{ticket_id}/audit`                                           | `TICKETS_READ`  |

Org context arrives via `X-Yaaos-Org-Slug` (RouteSecurity.ORG_SCOPED).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.audit_log import list_for_entity
from app.core.auth import Action, org_id_var
from app.core.sessions import require
from app.core.webserver import RouteSpec, register_routes
from app.domain.tickets.service import (
    Ticket,
    TicketFilter,
    TicketNotFoundError,
    get,
    list_tickets,
)

router = APIRouter(dependencies=[Depends(require(Action.TICKETS_READ))])


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


def _org() -> UUID:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    return org_id


@router.get("/dashboard")
async def dashboard() -> dict[str, Any]:
    """Single-query Dashboard projection per E2a.3.

    Response shape:
    `{stats: {in_flight, hitl_pending, completed_today, failed_today},
       in_flight: [TicketRow ≤10],
       needs_attention: [TicketRow ≤5]}`.

    Avoids the SPA making three `/api/tickets?status=…` calls in a tight
    polling loop. `t.status` is the 6-state vocab
    (pending / running / hitl / done / failed / cancelled); precise hitl/failed
    counts depend on the workflow-state projection landing on every
    transition.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    org_id = _org()
    # Pull a reasonable window — last 30 days plus everything active. For
    # POC, listing recent tickets is cheap enough; refinement later.
    items = await list_tickets(TicketFilter(sort="updated_desc"), org_id=org_id, limit=200)

    in_flight: list[Ticket] = [t for t in items if t.status == "running"]
    needs_attention: list[Ticket] = [
        t
        for t in items
        if t.findings_count > 0 and t.status == "done" and t.max_severity in ("should_fix", "blocker")
    ]

    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    completed_today = sum(
        1 for t in items if t.status == "done" and (t.updated_at or t.created_at) >= today_start
    )
    failed_today = sum(
        1 for t in items if t.status == "failed" and (t.updated_at or t.created_at) >= today_start
    )

    return {
        "stats": {
            "in_flight": len(in_flight),
            "hitl_pending": sum(1 for t in items if t.status == "hitl"),
            "completed_today": completed_today,
            "failed_today": failed_today,
        },
        "in_flight": [t.model_dump(mode="json") for t in in_flight[:10]],
        "needs_attention": [t.model_dump(mode="json") for t in needs_attention[:5]],
    }


@router.get("")
async def list_(
    repo_external_id: list[str] | None = Query(default=None),
    status: list[str] | None = Query(default=None),
    q: str | None = Query(default=None),
    sort: str = Query(default="updated_desc"),
    cursor: str | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    limit: int = Query(default=50, le=200),
) -> dict[str, Any]:
    """List tickets per the contract.

    Returns `{items, next_cursor}` instead of a bare array so the SPA can
    drive Load-more pagination. `next_cursor` is null today — POC uses
    naive limit pagination; opaque-cursor support lands when the result
    sets grow.
    """
    filter_ = TicketFilter(
        repo_external_ids=repo_external_id,
        statuses=status,  # type: ignore[arg-type]
        q=q,
        sort=sort,  # type: ignore[arg-type]
        cursor=cursor,
        created_after=created_after,
        created_before=created_before,
    )
    items = await list_tickets(filter_, org_id=_org(), limit=limit)
    return {
        "items": [t.model_dump(mode="json") for t in items],
        "next_cursor": None,
    }


@router.get("/{ticket_id}")
async def detail(ticket_id: UUID) -> dict[str, Any]:
    """Per-ticket detail with the enrichment: `builder` (the trigger identity).

    Returns the Ticket pydantic fields plus:
    - `builder: {kind, user_id?, display_name, avatar_url?}` — `kind="user"`
       when the ticket's PR has an `author_login`; `kind="system"` when
       yaaos triggered the run with no human attribution.
    """
    org_id = _org()
    try:
        ticket = await get(ticket_id, org_id=org_id)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")

    builder: dict[str, Any] = (
        {
            "kind": "user",
            "user_id": None,
            "display_name": ticket.author_login,
            "avatar_url": None,
        }
        if ticket.author_login
        else {"kind": "system", "display_name": "yaaos"}
    )

    payload = ticket.model_dump(mode="json")
    payload["builder"] = builder
    return payload


@router.get("/{ticket_id}/audit")
async def audit(ticket_id: UUID, limit: int = 200) -> list[dict[str, Any]]:
    """Aggregated timeline: ticket events + PR events for the ticket's PR."""
    org_id = _org()
    try:
        ticket = await get(ticket_id, org_id=org_id)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    entries = await list_for_entity("ticket", ticket_id, org_id=org_id, limit=limit)
    if ticket.pr_id is not None:
        entries.extend(await list_for_entity("pull_request", ticket.pr_id, org_id=org_id, limit=limit))
    entries.sort(key=lambda e: e.created_at, reverse=True)
    return [e.model_dump(mode="json") for e in entries[:limit]]


register_routes(RouteSpec(module_name="tickets", router=router))
