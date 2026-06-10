"""SQLAlchemy models for the reviewer's persistent state.

- `ReviewRow` (`reviews`) — one row per PR run.
- `FindingRow` (`findings`) — canonical finding with severity/confidence/category
  + optional anchor + `finding_display_id` (per-PR monotonic handle).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReviewRow(Base):
    """One review run per PR.

    Identity = `id` UUID. `sequence_number` is a per-PR ordinal (1, 2, 3, …)
    assigned at insert time inside the PG advisory lock so concurrent inserts
    serialize cleanly.
    """

    __tablename__ = "reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    pr_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pull_requests.id"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    trigger_reason: Mapped[str] = mapped_column(String, nullable=False, server_default="pr_ready")
    destination: Mapped[str] = mapped_column(String, nullable=False, server_default="vcs")
    scope_kind: Mapped[str] = mapped_column(String, nullable=False, server_default="full")
    commit_sha_at_start: Mapped[str | None] = mapped_column(String, nullable=True)
    # FK → coding_agent_runs(id); nullable — reviews from non-review workflows
    # (and rows created before this column existed) have no run.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("coding_agent_runs.id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("pr_id", "sequence_number", name="uq_reviews_pr_sequence"),
        Index("ix_reviews_pr_status_created", "pr_id", "status", "created_at"),
        Index("ix_reviews_pr_sequence", "pr_id", "sequence_number"),
    )


class FindingRow(Base):
    """Canonical finding. One row per finding per PR.

    `finding_display_id` is a per-PR monotonic integer (1, 2, 3, …) assigned at
    creation. The user-visible handle is `<category-prefix>-<finding_display_id>`.
    `file` and `line` are nullable — general (PR-wide) findings carry no anchor.
    """

    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    pr_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pull_requests.id"), nullable=False
    )
    review_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("reviews.id"), nullable=False
    )
    finding_display_id: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(String, nullable=False)
    rule_violated: Mapped[str] = mapped_column(String, nullable=False)
    rule_source: Mapped[str] = mapped_column(String, nullable=False)
    suggested_fix: Mapped[str] = mapped_column(String, nullable=False)
    file: Mapped[str | None] = mapped_column(String, nullable=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("pr_id", "finding_display_id", name="uq_findings_pr_display_id"),
        Index("ix_findings_org", "org_id"),
        Index("ix_findings_pr", "pr_id"),
    )
