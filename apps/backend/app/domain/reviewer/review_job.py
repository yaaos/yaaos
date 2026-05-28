"""Pydantic value objects for the `review_jobs` row + scheduler input.

Lives apart from the runner so the read-side endpoints (`web.py`,
`__init__.py`) can import the API shape without depending on the runner
code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.domain.reviewer.models import ReviewRow


class ReviewJobInput(BaseModel):
    """Argument bag for the scheduler entry point.

    Carries the four ids needed to start a review run: the
    review_jobs row id (the `review_job_id`), the originating ticket,
    the org for scoping, and a debounce window the scheduler honors
    before kicking off the runner."""

    review_job_id: UUID
    ticket_id: UUID
    org_id: UUID
    debounce_seconds: int


class ReviewJob(BaseModel):
    """Read-side projection of a `review_jobs` row.

    The SPA reads this via the `jobs_by_ticket` endpoint in
    `reviewer/web.py`. The workflow-engine path doesn't emit this
    shape — `workflow_executions` rows are the canonical job record.
    """

    id: UUID
    org_id: UUID
    pr_id: UUID
    status: str
    trigger_reason: str
    destination: str
    skip_reason: str | None
    scheduled_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    last_heartbeat_at: datetime | None
    current_step: str | None
    prompt_hash: str | None
    lessons_applied: list[UUID] | None
    tokens_in: int | None
    tokens_out: int | None
    duration_s: int | None
    error_message: str | None
    review_external_id: str | None
    findings: list[dict[str, Any]] | None
    activity_log: list[dict[str, Any]]
    model: str | None
    effort: str | None

    @classmethod
    def from_row(cls, row: ReviewRow) -> ReviewJob:
        return cls(
            id=row.id,
            org_id=row.org_id,
            pr_id=row.pr_id,
            status=row.status,
            trigger_reason=row.trigger_reason,
            destination=row.destination,
            skip_reason=row.skip_reason,
            scheduled_at=row.scheduled_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            last_heartbeat_at=row.last_heartbeat_at,
            current_step=row.current_step,
            prompt_hash=row.prompt_hash,
            lessons_applied=row.lessons_applied,
            tokens_in=row.tokens_in,
            tokens_out=row.tokens_out,
            duration_s=row.duration_s,
            error_message=row.error_message,
            review_external_id=row.review_external_id,
            findings=row.findings,
            activity_log=row.activity_log or [],
            model=row.model,
            effort=row.effort,
        )


__all__ = ["ReviewJob", "ReviewJobInput"]
