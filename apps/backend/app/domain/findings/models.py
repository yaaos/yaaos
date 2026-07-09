"""SQLAlchemy row owned by `domain/findings`.

`FindingRow` is the durable ticket-level entity, materialized the moment a
review iteration reports it — including findings the fix loop resolves a
minute later. One source of truth for finding content; engine-side
`loop_state` (on `stage_executions`) holds only references + verdicts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FindingRow(Base):
    """One durable finding. `id` is app-minted (engine's uuid7 at first
    report) — no server_default. `severity` is immutable after creation.
    """

    __tablename__ = "findings"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    ticket_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    source_run_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_stage_name: Mapped[str] = mapped_column(String, nullable=False)
    source_stage_execution_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    first_seen_iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    display_prefix: Mapped[str] = mapped_column(String, nullable=False)
    display_id: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    code_file: Mapped[str | None] = mapped_column(String, nullable=True)
    code_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_section: Mapped[str | None] = mapped_column(String, nullable=True)
    defect_in_artifact: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="open")
    status_events: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    defended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_comment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("severity IN ('blocker','should_fix','nit')", name="ck_findings_severity"),
        CheckConstraint("status IN ('open','resolved','dismissed')", name="ck_findings_status"),
        UniqueConstraint("ticket_id", "display_id", name="uq_findings_ticket_display_id"),
        Index("ix_findings_ticket_status", "ticket_id", "status"),
        Index("ix_findings_stage_execution", "source_stage_execution_id"),
        Index("ix_findings_external_comment", "org_id", "external_comment_id"),
    )
