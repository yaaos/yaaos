"""SQLAlchemy model for `workspaces`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WorkspaceRow(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    # owning agent (`workspace_agents.id`). Set at provision-dispatch to the agent
    # that ran `ProvisionWorkspace` — that pod owns this workspace for its whole
    # life, so every post-provision command routes back to it. NOT NULL; every
    # workspace row is created by and belongs to an agent. FK enforces referential
    # integrity; ON DELETE RESTRICT so a `workspace_agents` row cannot be deleted
    # while any workspace still references it. A workspace never outlives its
    # owning agent — RESTRICT keeps the NOT NULL invariant intact (SET NULL would
    # try to NULL a NOT NULL column and fail the delete anyway).
    owning_agent_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspace_agents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    # single-flight claim: only one in-flight AgentCommand per workspace.
    # Set by `try_claim()`; cleared by `release_claim()` after the terminal
    # event has been observed (NOT before — failure-report-precedes-disposal).
    current_command_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    # idle-timeout sweep horizon. The reaper marks any workspace
    # `active` past this window as `expired` so its cleanup workflow can run.
    max_idle_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="600")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    destroy_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_destroy_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_destroy_error: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_workspaces_status_expires", "status", "expires_at"),
        Index("ix_workspaces_org_created", "org_id", "created_at"),
        Index("ix_workspaces_owning_agent_id", "owning_agent_id"),
    )
