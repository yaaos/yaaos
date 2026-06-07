"""SQLAlchemy models for the claude_code plugin."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, LargeBinary, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ClaudeCodeSettingsRow(Base):
    __tablename__ = "claude_code_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, unique=True)
    encrypted_anthropic_api_key: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    default_model: Mapped[str | None] = mapped_column(String, nullable=True)
    cli_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ClaudeCodeRepoRow(Base):
    """Per-(org, repo) skill manifest cache for the claude_code plugin.

    `skills` is the last-enumerated `SkillManifestEntry[]` (overwritten by
    Refresh). `enumerated_at` is null until the first successful enumeration.
    No `status` column — enumeration state lives in the workflow execution.
    """

    __tablename__ = "claude_code_repos"
    __table_args__ = (UniqueConstraint("org_id", "repo_external_id", name="uq_claude_code_repos_org_repo"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    repo_external_id: Mapped[str] = mapped_column(String, nullable=False)
    skills: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    enumerated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
