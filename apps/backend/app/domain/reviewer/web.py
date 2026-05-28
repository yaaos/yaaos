"""HTTP routes for review-job + durable-findings operations.

Per-endpoint Action gating — `REVIEWER_READ` on GETs, `REVIEWER_WRITE` on
POSTs. Org context arrives via `X-Org-Slug`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import Action, org_id_var
from app.core.database import session
from app.core.sessions import require
from app.core.webserver import RouteSpec, register_routes
from app.domain import tickets
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.review_job import ReviewJob
from app.domain.reviewer.service import (
    all_conversations_view,
    list_findings_view,
)

router = APIRouter()


class RereviewRequest(BaseModel):
    ticket_id: UUID


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


def _org() -> UUID:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    return org_id


@router.post("/rereview", dependencies=[Depends(require(Action.REVIEWER_WRITE))])
async def rereview_ticket(req: RereviewRequest) -> dict[str, Any]:
    """Re-review a ticket — drives `pr_review_v1` via the workflow engine.

    The SPA's only contract with this endpoint is the `scheduled_count`
    field; the response carries `workflow_execution_id` so the caller can
    poll workflow state if desired.
    """
    org_id = _org()
    try:
        await tickets.get(req.ticket_id, org_id=org_id)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")

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


class _PushBackRequest(BaseModel):
    reason: str


@router.post(
    "/findings/{finding_id}/ack",
    dependencies=[Depends(require(Action.REVIEWER_WRITE))],
)
async def ack_finding(finding_id: UUID) -> dict[str, Any]:
    """: SPA "Ack" button. Marks the finding as `intentional` —
    Builder sees this and accepts it for future reviews."""
    return await _record_ack(finding_id, kind="intentional", rationale="ack")


@router.post(
    "/findings/{finding_id}/push-back",
    dependencies=[Depends(require(Action.REVIEWER_WRITE))],
)
async def push_back_finding(finding_id: UUID, req: _PushBackRequest) -> dict[str, Any]:
    """: SPA "Push back" button. Marks the finding as `wontfix` and
    records the Builder's reason in the audit trail."""
    reason = req.reason.strip()
    if len(reason) < 10:
        raise HTTPException(status_code=422, detail="reason must be ≥10 chars")
    return await _record_ack(finding_id, kind="wontfix", rationale=reason)


async def _record_ack(finding_id: UUID, *, kind: str, rationale: str) -> dict[str, Any]:
    """Shared body: load aggregate, transition the finding, save.

    The aggregate's `acknowledge()` references a `made_by_message_id` that
    has a FK to `comment_messages`. For an HTTP-driven ack there's no
    inbound reply message, so we synthesize one: open a thread on the
    finding (or reuse the existing thread) and append a single
    `kind="human"` message whose body is the Builder's rationale. The
    aggregate then transitions atomically; one `repo.save()` persists
    the thread + message + ack.
    """
    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.core.auth import user_id_var  # noqa: PLC0415
    from app.domain.reviewer.models import FindingRow  # noqa: PLC0415

    org_id = _org()
    async with session() as s:
        finding = (
            await s.execute(_select(FindingRow).where(FindingRow.id == finding_id))
        ).scalar_one_or_none()
        if finding is None or finding.org_id != org_id:
            raise HTTPException(status_code=404, detail="finding not found")
        repo = SqlAlchemyAggregateRepository(s)
        aggregate = await repo.load(pr_id=finding.pr_id, org_id=org_id)
        actor_id = user_id_var.get()
        actor_label = str(actor_id) if actor_id else "system"

        # Find or open a thread for this finding.
        thread = next((t for t in aggregate.threads if t.finding_id == finding_id), None)
        if thread is None:
            thread = aggregate.open_thread_for_finding(finding_id)

        msg = aggregate.append_message(
            thread_id=thread.id,
            author_kind="human",
            author_external_id=actor_label,
            external_comment_id=f"http-ack-{finding_id}",
            body=rationale,
        )

        try:
            aggregate.acknowledge(
                finding_id=finding_id,
                kind=kind,  # type: ignore[arg-type]
                rationale=rationale,
                made_by_external_id=actor_label,
                made_by_message_id=msg.id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="finding not in aggregate")
        await repo.save(aggregate)
        await s.commit()
    return {"finding_id": str(finding_id), "state": "acked" if kind == "intentional" else "pushed_back"}


@router.get("/jobs/by-ticket/{ticket_id}", dependencies=[Depends(require(Action.REVIEWER_READ))])
async def jobs_by_ticket(ticket_id: UUID) -> list[ReviewJob]:
    """Per-ticket review history."""
    from app.domain.reviewer.workflow_review_view import (  # noqa: PLC0415
        list_review_jobs_for_ticket as list_workflow_jobs,
    )

    org_id = _org()
    try:
        t = await tickets.get(ticket_id, org_id=org_id)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")

    pr_id_for_projection = t.pr_id or UUID(int=0)
    rows = await list_workflow_jobs(ticket_id, pr_id=pr_id_for_projection, org_id=org_id)
    rows.sort(key=lambda j: j.scheduled_at, reverse=True)
    return rows


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
    """List open + acknowledged findings for the ticket's PR."""
    org_id = _org()
    try:
        t = await tickets.get(ticket_id, org_id=org_id)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t.pr_id is None:
        return []
    async with session() as s:
        repo = SqlAlchemyAggregateRepository(s)
        aggregate = await repo.load(pr_id=t.pr_id, org_id=org_id)
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


@router.get(
    "/conversations/by-ticket/{ticket_id}",
    dependencies=[Depends(require(Action.REVIEWER_READ))],
)
async def conversations_by_ticket(ticket_id: UUID) -> list[dict[str, Any]]:
    """All-Conversations cross-cut for the ticket's PR."""
    org_id = _org()
    try:
        t = await tickets.get(ticket_id, org_id=org_id)
    except tickets.TicketNotFoundError:
        raise HTTPException(status_code=404, detail="ticket not found")
    if t.pr_id is None:
        return []
    async with session() as s:
        repo = SqlAlchemyAggregateRepository(s)
        aggregate = await repo.load(pr_id=t.pr_id, org_id=org_id)
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


@router.get(
    "/reviews/by-ticket/{ticket_id}",
    dependencies=[Depends(require(Action.REVIEWER_READ))],
)
async def reviews_by_ticket(ticket_id: UUID) -> list[dict[str, Any]]:
    """Per-review timeline metadata."""
    from sqlalchemy import desc, select  # noqa: PLC0415

    from app.domain.reviewer.models import ReviewRow  # noqa: PLC0415

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


@router.get(
    "/findings/{finding_id}/thread",
    dependencies=[Depends(require(Action.REVIEWER_READ))],
)
async def thread_by_finding(finding_id: UUID) -> dict[str, Any]:
    """Thread messages + ack banner for one finding."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.domain.reviewer.models import (  # noqa: PLC0415
        AcknowledgmentDecisionRow,
        CommentMessageRow,
        CommentThreadRow,
        FindingRow,
    )

    org_id = _org()
    async with session() as s:
        finding = (
            await s.execute(select(FindingRow).where(FindingRow.id == finding_id))
        ).scalar_one_or_none()
        if finding is None:
            raise HTTPException(status_code=404, detail="finding not found")
        if finding.org_id != org_id:
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
