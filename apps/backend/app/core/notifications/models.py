"""SQLAlchemy model for `notifications`.

One row per user-targeted event. Keyed for the primary read path
("unread + recent for me") and filter combinations (per-org / per-type).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NotificationRow(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    # `hitl_waiting` | `ticket_completed` | `ticket_failed` today; freeform
    # to keep future kinds zero-migration.
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Generic subject reference — both null or both set.
    subject_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Primary read path: "unread + recent for me".
        Index("notifications_user_read_created_idx", "user_id", "read_at", "created_at"),
        # Filter-combination index: per-user per-org per-type history.
        Index(
            "notifications_user_org_type_idx",
            "user_id",
            "org_id",
            "type",
            "created_at",
        ),
        # Dedup index: skip duplicate (user, type, subject) tuples.
        # NULLs are distinct in Postgres partial indexes — subject-less
        # notifications (subject_type IS NULL) bypass this index, i.e. they
        # are never deduplicated (checked in service.create instead).
        Index(
            "notifications_dedup_subject_idx",
            "user_id",
            "type",
            "subject_type",
            "subject_id",
            postgresql_where="subject_type IS NOT NULL",
        ),
    )
