"""Workflow-execution → `ReviewJob` projection.

`pr_review_v1` workflows run through `workflow_executions`. The
`/api/reviewer/metrics` endpoint surfaces the `ReviewJob`-shaped aggregate
the SPA reads, so this module projects `WorkflowExecutionSummary` into
`ReviewJob` fields.

Fields not tracked in `workflow_executions` (tokens, model, effort)
project as `None`.

Status mapping (workflow state → ReviewJob.status):
- `pending` → `queued`
- `running` / `awaiting_agent` / `awaiting_human` → `running`
- `done` → `posted`
- `failed` → `failed`
- `cancelled` → `cancelled`
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.database import session as db_session
from app.core.workflow import (
    WorkflowExecutionSummary,
    WorkflowState,
    list_all_execution_states,
    list_executions_for_ticket,
)
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


def project_workflow_to_review_job(
    summary: WorkflowExecutionSummary, *, pr_id: UUID, org_id: UUID
) -> ReviewJob:
    """Read one `WorkflowExecutionSummary` as a `ReviewJob`."""
    status = _STATE_TO_STATUS.get(summary.state, summary.state)
    return ReviewJob(
        id=summary.id,
        org_id=org_id,
        pr_id=pr_id,
        status=status,
        trigger_reason=summary.workflow_name,
        destination="vcs",
        scope_kind="full",
        commit_sha_at_start=None,
        sequence_number=0,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
        scheduled_at=summary.created_at,
    )


async def list_review_jobs_for_ticket(ticket_id: UUID, *, pr_id: UUID, org_id: UUID) -> list[ReviewJob]:
    """Return all workflow-execution projections for one ticket."""
    async with db_session() as s:
        summaries = await list_executions_for_ticket(ticket_id, session=s)
    return [project_workflow_to_review_job(r, pr_id=pr_id, org_id=org_id) for r in summaries]


async def workflow_metrics_summary(*, org_id: UUID) -> dict[str, Any]:
    """Org-scoped counts-by-status over `workflow_executions`."""
    del org_id
    async with db_session() as s:
        states = await list_all_execution_states(session=s)
    statuses: dict[str, int] = {}
    posted = 0
    failed = 0
    for state in states:
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
