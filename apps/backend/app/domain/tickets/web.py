"""HTTP routes for tickets.

| Method | Path                            | Action          |
|--------|---------------------------------|-----------------|
| GET    | `/api/tickets`                  | `TICKETS_READ`  |
| GET    | `/api/tickets/{ticket_id}`      | `TICKETS_READ`  |
| GET    | `/api/tickets/{ticket_id}/audit`| `TICKETS_READ`  |

Org context arrives via `X-Org-Slug` (RouteSecurity.ORG_SCOPED).
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
    polling loop. `t.status` is the 5-state vocab post-collapse
    (running / hitl / done / failed / cancelled); precise hitl/failed
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
        if t.findings_count > 0 and t.status == "done" and t.max_severity in ("medium", "high")
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


@router.post("/{ticket_id}/hitl/respond")
async def hitl_respond(ticket_id: UUID, response: dict[str, Any]) -> dict[str, Any]:
    """Submit a HITL response. Resolves the open `PendingHumanDecisionRow`
    for the ticket's most recent awaiting-human workflow execution and
    re-enqueues the routing step via `core.workflow.resume_hitl`.

    Request body: opaque dict — passes through to the workflow engine's
    `resume_hitl(response=...)`. The SPA's HITL renderer shapes this
    per the prompt's discriminated-union schema (E2a.4).

    Returns `{stage, next_state}` where `next_state` is the workflow
    state immediately after the resume.
    """
    from app.core.database import session as _db_session  # noqa: PLC0415
    from app.core.workflow import get_awaiting_human_execution, resume_hitl  # noqa: PLC0415

    org_id = _org()
    try:
        await get(ticket_id, org_id=org_id)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")

    async with _db_session() as s:
        wfx = await get_awaiting_human_execution(ticket_id, session=s)
        if wfx is None:
            raise HTTPException(status_code=404, detail="no pending HITL on ticket")
        resolved = await resume_hitl(str(wfx.id), response=response, session=s)
        if not resolved:
            raise HTTPException(status_code=409, detail="HITL decision already resolved")
        await s.commit()
        # Re-fetch the updated summary after commit so next_state is current.
        from app.core.workflow import get_execution_summary  # noqa: PLC0415

        refreshed = await get_execution_summary(wfx.id, session=s)

    return {
        "stage": wfx.workflow_name,
        "next_state": refreshed.state if refreshed else wfx.state,
    }


@router.get("/{ticket_id}/hitl/history")
async def hitl_history(ticket_id: UUID) -> list[dict[str, Any]]:
    """List past HITL exchanges (prompt + response + timestamps) for the
    ticket per E2a.4 HITL tab "History" subsection.

    Joins `pending_human_decisions` against the ticket's
    `workflow_executions` rows. Newest exchange first.
    """
    from app.core.database import session as _db_session  # noqa: PLC0415
    from app.core.workflow import list_hitl_history  # noqa: PLC0415

    org_id = _org()
    try:
        await get(ticket_id, org_id=org_id)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")

    async with _db_session() as s:
        entries = await list_hitl_history(ticket_id, session=s)

    return [
        {
            "id": str(e.id),
            "workflow_execution_id": str(e.workflow_execution_id),
            "question_payload": e.question_payload,
            "resolution_payload": e.resolution_payload,
            "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]


@router.get("/{ticket_id}")
async def detail(ticket_id: UUID) -> dict[str, Any]:
    """Per-ticket detail with the enrichment: `stages[]` (projected from
    `workflow_executions`) + `builder` (the trigger identity).

    Returns the Ticket pydantic fields plus:
    - `stages: [{name, state, attempt_count, current_attempt, started_at,
       completed_at, workflow_execution_id}]` — one entry per workflow run
       on the ticket, newest first.
    - `builder: {kind, user_id?, display_name, avatar_url?}` — `kind="user"`
       when the ticket's PR has an `author_login`; `kind="system"` when
       yaaos triggered the run with no human attribution.
    """
    from app.core.database import session as _db_session  # noqa: PLC0415
    from app.core.workflow import list_executions_for_ticket  # noqa: PLC0415

    org_id = _org()
    try:
        ticket = await get(ticket_id, org_id=org_id)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")

    async with _db_session() as s:
        wfx_summaries = await list_executions_for_ticket(ticket_id, session=s)

    stages = [
        {
            "name": w.workflow_name,
            "state": w.state,
            "attempt_count": 1,  # POC: one attempt per execution row.
            "current_attempt": 1,
            "started_at": w.created_at.isoformat() if w.created_at else None,
            "completed_at": (
                w.updated_at.isoformat() if w.state in ("done", "failed", "cancelled") else None
            ),
            "workflow_execution_id": str(w.id),
        }
        for w in wfx_summaries
    ]

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
    payload["stages"] = stages
    payload["builder"] = builder
    return payload


@router.get("/{ticket_id}/audit")
async def audit(ticket_id: UUID, limit: int = 200) -> list[dict[str, Any]]:
    """Aggregated timeline: ticket + its PR + every review_job for that PR
    + every finding raised against that PR (so reply-flow events like
    `finding_acknowledged` surface in the ticket-level audit feed)."""
    from app.domain import reviewer as reviewer_mod  # noqa: PLC0415

    org_id = _org()
    try:
        ticket = await get(ticket_id, org_id=org_id)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    entries = await list_for_entity("ticket", ticket_id, org_id=org_id, limit=limit)
    if ticket.pr_id is not None:
        entries.extend(await list_for_entity("pull_request", ticket.pr_id, org_id=org_id, limit=limit))
        jobs = await reviewer_mod.list_review_jobs_for_pr(ticket.pr_id, org_id=org_id)
        for j in jobs:
            entries.extend(await list_for_entity("review_job", j.id, org_id=org_id, limit=limit))
        findings = await reviewer_mod.list_findings_for_pr(ticket.pr_id, org_id=org_id, include_terminal=True)
        for f in findings:
            entries.extend(await list_for_entity("finding", f.id, org_id=org_id, limit=limit))
    entries.sort(key=lambda e: e.created_at, reverse=True)
    return [e.model_dump(mode="json") for e in entries[:limit]]


register_routes(RouteSpec(module_name="tickets", router=router))
