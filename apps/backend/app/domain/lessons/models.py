"""SQLAlchemy model for `lessons`.

Lessons are keyed by `(plugin_id, repo_external_id)` — a stable string identity
the VCS plugin produces. There is no yaaos-side `repos` table; the GitHub App's
install picks the access scope, and yaaos learns about repos as PRs arrive.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LessonRow(Base):
    __tablename__ = "lessons"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    plugin_id: Mapped[str] = mapped_column(String, nullable=False, server_default="github")
    repo_external_id: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    source_pr_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Nullable: rows created before the column add (and rows created by
    # the workspace agent / system reviewer) have no user attribution.
    created_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("lessons_repo_idx", "org_id", "plugin_id", "repo_external_id"),)
