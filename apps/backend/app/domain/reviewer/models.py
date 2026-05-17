"""SQLAlchemy models for reviewer_agents, review_jobs, posted_comments."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReviewerAgentRow(Base):
    __tablename__ = "reviewer_agents"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    prompt_text: Mapped[str] = mapped_column(String, nullable=False)
    coding_agent_plugin_id: Mapped[str] = mapped_column(String, nullable=False, default="claude_code")
    agent_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_built_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_reviewer_agents_org_name"),)


class ReviewJobRow(Base):
    __tablename__ = "review_jobs"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    pr_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pull_requests.id"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("reviewer_agents.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False, default="review")
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    # Why this review was scheduled. Values: `pr_ready`, `pr_synchronized`,
    # `rereview_command`, `ui_rereview`. Future: `implementer_loop` once an
    # implementer module exists to call `run_review`.
    triggered_by: Mapped[str] = mapped_column(String, nullable=False, server_default="pr_ready")
    # Where the review result went. `vcs` (today: posted via the VCS plugin).
    # Future: `caller` when `run_review` returns findings without posting (used
    # by implementer agents that have no PR to post against yet).
    destination: Mapped[str] = mapped_column(String, nullable=False, server_default="vcs")
    skip_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_step: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    lessons_applied: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=True
    )
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    review_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    findings: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    parent_comment_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reply_body: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_review_jobs_pr_status_created", "pr_id", "status", "created_at"),
        Index("ix_review_jobs_status_heartbeat", "status", "last_heartbeat_at"),
    )


class PostedCommentRow(Base):
    __tablename__ = "posted_comments"

    external_comment_id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    pr_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pull_requests.id"), nullable=False
    )
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("review_jobs.id"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("reviewer_agents.id"), nullable=False
    )
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
