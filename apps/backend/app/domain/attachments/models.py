"""SQLAlchemy row owned by `domain/attachments`."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Text, desc, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.domain.attachments.types import Attachment, AttachmentMeta


class TicketAttachmentRow(Base):
    """One user-supplied ticket input document.

    `produced_by_skill` and related frontmatter columns are NULL when the
    body carries no valid frontmatter — a context-only attachment that still
    reaches the agent as context but is never matched by the adoption logic.
    `attached_at` is THE precedence key for adoption matching.
    """

    __tablename__ = "ticket_attachments"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    ticket_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Frontmatter-populated columns — NULL when no frontmatter or parse fails.
    produced_by_skill: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    produced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    repo_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    produced_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Attacher metadata.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    attached_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    attached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_ticket_attachments_match", "ticket_id", "produced_by_skill", desc("attached_at")),
    )

    def to_meta(self) -> AttachmentMeta:
        return AttachmentMeta(
            id=self.id,
            filename=self.filename,
            produced_by_skill=self.produced_by_skill,
            skill_version=self.skill_version,
            artifact_type=self.artifact_type,
            repo_commit=self.repo_commit,
            note=self.note,
            attached_by=self.attached_by,
            attached_at=self.attached_at,
        )

    def to_attachment(self) -> Attachment:
        return Attachment(
            id=self.id,
            org_id=self.org_id,
            ticket_id=self.ticket_id,
            filename=self.filename,
            body=self.body,
            produced_by_skill=self.produced_by_skill,
            skill_version=self.skill_version,
            artifact_type=self.artifact_type,
            produced_at=self.produced_at,
            repo_commit=self.repo_commit,
            produced_from=self.produced_from,
            note=self.note,
            attached_by=self.attached_by,
            attached_at=self.attached_at,
        )
