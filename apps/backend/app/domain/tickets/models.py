"""SQLAlchemy model for `tickets`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TicketRow(Base):
    __tablename__ = "tickets"
    # One ticket per (org, source, external id). The github intake type's
    # PR-opened branch upserts on this key, so concurrent webhook deliveries
    # for the same PR collapse to a single row.
    __table_args__ = (
        UniqueConstraint("org_id", "source", "source_external_id", name="uq_tickets_org_source_external"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String, nullable=False, default="github_pr")
    source_external_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    plugin_id: Mapped[str] = mapped_column(String, nullable=False, server_default="github")
    repo_external_id: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    pr_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    # ticket type. `pr_review` is the value in use; other types reuse the
    # same row shape.
    type: Mapped[str] = mapped_column(String, nullable=False, server_default="pr_review")
    # idempotency key for intake-driven creation. Same key + same type →
    # the existing ticket is returned. Sparse-unique; rows created without
    # a key leave it NULL.
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    # optional payload bag carrying intake-time parameters that the
    # workflow's first step consumes. Stays JSONB so future ticket types add
    # fields without schema churn.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Soft ref (no DB constraint) to the pipeline_runs row currently driving
    # this ticket.
    current_run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    # Per-ticket work branch. Intake-supplied (PR tickets: the head branch
    # LABEL — provenance/display only, checkout pins the head SHA) or minted
    # at creation (yaaos/<slugified-title>-<shortid>). Nullable for tickets
    # predating branch minting.
    branch_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Denormalized rollup written by domain/findings after each finding
    # report or verdict. Avoids a cross-module import at list time.
    findings_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_severity: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
