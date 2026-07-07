"""SQLAlchemy row owned by `domain/pr_review`.

`PRCommentRow` tracks one inbound free-text PR comment: classification,
finding anchor, and single-batch claim. Lifecycle is derived, not a status
column — NULL classification = awaiting classify, `unclear` = terminal
(canned reply), classified+unclaimed = waiting, claimed = in a run.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PRCommentRow(Base):
    """One inbound free-text PR comment yaaos tracks."""

    __tablename__ = "pr_comments"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    ticket_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    comment_external_id: Mapped[str] = mapped_column(String, nullable=False)
    in_reply_to_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    author_login: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    finding_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pipeline_findings.id"), nullable=True
    )
    classification: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_by_run_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "classification IS NULL OR classification IN ('question','claims_fixed','dispute','unclear')",
            name="ck_pr_comments_classification",
        ),
        UniqueConstraint("org_id", "comment_external_id", name="uq_pr_comments_org_external"),
        Index(
            "ix_pr_comments_waiting",
            "ticket_id",
            postgresql_where=text(
                "claimed_by_run_id IS NULL AND classification IS NOT NULL AND classification != 'unclear'"
            ),
        ),
    )
