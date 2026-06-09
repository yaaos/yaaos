"""Pydantic value objects for the `reviews` row + scheduler input.

Lives apart from the runner so the read-side endpoints (`web.py`,
`__init__.py`) can import the API shape without depending on the runner
code.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ReviewJobInput(BaseModel):
    """Argument bag for the scheduler entry point."""

    ticket_id: UUID
    org_id: UUID
    debounce_seconds: int = 0


class ReviewJob(BaseModel):
    """Read-side projection of a `reviews` row for the ticket history API."""

    id: UUID
    org_id: UUID
    pr_id: UUID
    status: str
    trigger_reason: str
    destination: str
    scope_kind: str
    commit_sha_at_start: str | None
    sequence_number: int
    created_at: datetime
    updated_at: datetime
    scheduled_at: datetime | None = None

    @classmethod
    def from_row(cls, row: object) -> ReviewJob:  # type: ignore[override]
        from app.domain.reviewer.models import ReviewRow  # noqa: PLC0415

        r: ReviewRow = row  # type: ignore[assignment]
        return cls(
            id=r.id,
            org_id=r.org_id,
            pr_id=r.pr_id,
            status=r.status,
            trigger_reason=r.trigger_reason,
            destination=r.destination,
            scope_kind=r.scope_kind,
            commit_sha_at_start=r.commit_sha_at_start,
            sequence_number=r.sequence_number,
            created_at=r.created_at,
            updated_at=r.updated_at,
            scheduled_at=r.created_at,
        )


__all__ = ["ReviewJob", "ReviewJobInput"]
