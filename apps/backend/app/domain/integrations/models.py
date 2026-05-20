"""SQLAlchemy model for `domain/integrations` — `mcp_credentials`.

One row per `(org_id, provider)`. Access + refresh tokens are encrypted via
`core/secrets`. `last_refresh_status` is `"ok"` after the most recent
successful refresh / validate; `"failed"` after refresh or scheduled health
check failed. Six broken-creds surfaces (banner, email, audit row, etc.)
key off this column.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class McpCredentialRow(Base):
    """Per-(org, provider) hosted-MCP OAuth credential + per-tool allowlist."""

    __tablename__ = "mcp_credentials"

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    # Empty array = read-only (all read tools allowed, no write tools allowed).
    # Non-empty array = write tools in the list are permitted.
    allowed_tools: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Display string the OAuth flow returned (email / handle); never used as an auth principal.
    upstream_identity: Mapped[str | None] = mapped_column(String, nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "ok" or "failed". Null until first refresh / health-check writes it.
    last_refresh_status: Mapped[str | None] = mapped_column(String(8), nullable=True)
    last_refresh_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
