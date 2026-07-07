"""Value objects for `domain/pr_review`."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.domain.pr_review.models import PRCommentRow

CommentClassification = Literal["question", "claims_fixed", "dispute", "unclear"]


class InboundComment(BaseModel):
    """VCS-agnostic wire input from the plugin."""

    external_id: str
    author_login: str
    body: str
    in_reply_to_external_id: str | None = None


class PRComment(BaseModel):
    """Domain value object for one tracked PR comment."""

    id: UUID
    org_id: UUID
    ticket_id: UUID
    comment_external_id: str
    in_reply_to_external_id: str | None
    author_login: str
    body: str
    finding_id: UUID | None
    classification: CommentClassification | None
    claimed_by_run_id: UUID | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: PRCommentRow) -> PRComment:
        return cls(
            id=row.id,
            org_id=row.org_id,
            ticket_id=row.ticket_id,
            comment_external_id=row.comment_external_id,
            in_reply_to_external_id=row.in_reply_to_external_id,
            author_login=row.author_login,
            body=row.body,
            finding_id=row.finding_id,
            classification=row.classification,  # type: ignore[arg-type]
            claimed_by_run_id=row.claimed_by_run_id,
            created_at=row.created_at,
        )
