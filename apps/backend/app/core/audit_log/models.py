"""SQLAlchemy model for `audit_entries`. Owned by core/audit_log."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuditEntryRow(Base):
    __tablename__ = "audit_entries"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    entity_kind: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    actor_kind: Mapped[str] = mapped_column(String, nullable=False)
    actor_login: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_agent_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    # M02 — additional actor-kind discriminators. `user` populates actor_user_id;
    # `workspace` populates actor_workspace_id; `sso` populates only actor_login
    # (the IdP-asserted email) since no domain id exists.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    actor_workspace_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_audit_entries_entity_timeline", "entity_kind", "entity_id", "created_at"),
        Index("ix_audit_entries_org_created", "org_id", "created_at"),
    )
