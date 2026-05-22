"""Workflow-execution → `ReviewJob` projection.

During the queue.py dismantle (slices 40-50), the legacy `review_jobs`
table stops receiving writes from new code paths — `pr_review_v1` /
`incremental_review_v1` workflows run through `workflow_executions`
instead. The SPA's existing `/api/reviewer/jobs/by-ticket/{id}` +
`/api/reviewer/metrics` endpoints still return the legacy `ReviewJob`
shape so the UI doesn't need a redesign mid-flight.

This module projects `WorkflowExecutionRow` into the same `ReviewJob`
fields. Lossy by design — `workflow_executions` doesn't track
per-finding output, token counts, or model/effort settings; those
fields project as `None`. The SPA already tolerates `None` because the
legacy queue path filled them lazily too.

Status mapping (workflow state → ReviewJob.status):
- `pending` → `queued`
- `running` / `awaiting_agent` / `awaiting_human` → `running`
- `done` → `posted`
- `failed` → `failed`
- `cancelled` → `cancelled`

When the legacy `review_jobs` table is dropped (migration 019), the
read-side endpoints return only workflow-projected rows. Until then
they merge both sources.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.core.database import session as db_session
from app.core.workflow import WorkflowExecutionRow, WorkflowState
from app.domain.reviewer.review_job import ReviewJob

_STATE_TO_STATUS: dict[str, str] = {
    WorkflowState.PENDING.value: "queued",
    WorkflowState.RUNNING.value: "running",
    WorkflowState.AWAITING_AGENT.value: "running",
    WorkflowState.AWAITING_HUMAN.value: "running",
    WorkflowState.DONE.value: "posted",
    WorkflowState.FAILED.value: "failed",
    WorkflowState.CANCELLED.value: "cancelled",
}


def project_workflow_to_review_job(row: WorkflowExecutionRow, *, pr_id: UUID, org_id: UUID) -> ReviewJob:
    """Read one `WorkflowExecutionRow` as a `ReviewJob`. Fields not tracked
    in `workflow_executions` (tokens, model, effort, findings, activity_log)
    project as None / empty list."""
    status = _STATE_TO_STATUS.get(row.state, row.state)
    return ReviewJob(
        id=row.id,
        org_id=org_id,
        pr_id=pr_id,
        status=status,
        trigger_reason=row.workflow_name,
        destination="vcs",
        skip_reason=None,
        scheduled_at=row.created_at,
        started_at=None,
        completed_at=row.updated_at if status in {"posted", "failed", "cancelled", "skipped"} else None,
        last_heartbeat_at=row.updated_at,
        current_step=row.current_step_id,
        prompt_hash=None,
        lessons_applied=None,
        tokens_in=None,
        tokens_out=None,
        duration_s=None,
        error_message=None,
        review_external_id=None,
        findings=None,
        activity_log=[],
        model=None,
        effort=None,
    )


async def list_review_jobs_for_ticket(ticket_id: UUID, *, pr_id: UUID, org_id: UUID) -> list[ReviewJob]:
    """Return all workflow-execution projections for one ticket, newest first."""
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(WorkflowExecutionRow)
                    .where(WorkflowExecutionRow.ticket_id == ticket_id)
                    .order_by(WorkflowExecutionRow.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    return [project_workflow_to_review_job(r, pr_id=pr_id, org_id=org_id) for r in rows]


async def workflow_metrics_summary(*, org_id: UUID) -> dict[str, Any]:
    """Org-scoped counts-by-status over `workflow_executions`.

    NOTE: `workflow_executions` has no `org_id` column (it's per-ticket;
    the org scope is enforced through the ticket). For the M01 POC we
    return ALL workflow_executions counts and let the legacy
    `metrics_summary` add the org-scoped review_jobs counts on top.
    """
    del org_id
    async with db_session() as s:
        rows = (await s.execute(select(WorkflowExecutionRow.state))).scalars().all()
    statuses: dict[str, int] = {}
    posted = 0
    failed = 0
    for state in rows:
        status = _STATE_TO_STATUS.get(state, state)
        statuses[status] = statuses.get(status, 0) + 1
        if status == "posted":
            posted += 1
        if status == "failed":
            failed += 1
    return {
        "review_jobs_by_status": statuses,
        "total_reviews_posted": posted,
        "failure_count": failed,
        "failure_rate": (failed / (posted + failed)) if (posted + failed) > 0 else 0.0,
    }


__all__ = [
    "list_review_jobs_for_ticket",
    "project_workflow_to_review_job",
    "workflow_metrics_summary",
]
