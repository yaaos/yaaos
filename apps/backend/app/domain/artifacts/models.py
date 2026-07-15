"""SQLAlchemy row owned by `domain/artifacts`.

One entity, one table: `Artifact` = one produced document. There is no
separate lineage/descriptor entity — the lineage ("the ticket's requirements
document") is the `(ticket_id, stage_name)` group, a composite key, not a row.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ArtifactRow(Base):
    """One produced document version. Bodies are immutable; `is_final` is the
    module's only mutation (flipped once by `mark_final` at boundary time)."""

    __tablename__ = "artifacts"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    ticket_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    stage_name: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("pipeline_runs.id"), nullable=False)
    stage_execution_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("stage_executions.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Set when the artifact body was synthesised from an attachment rather than
    # produced by a live coding-agent invocation (adoption path).  NULL on
    # engine-produced artifacts.
    adopted_from_attachment_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ticket_attachments.id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("ticket_id", "stage_name", "version", name="uq_artifacts_lineage_version"),
        Index("ix_artifacts_lineage_final", "ticket_id", "stage_name", "is_final"),
    )
