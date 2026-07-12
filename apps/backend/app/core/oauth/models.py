"""SQLAlchemy row models for `core/oauth` — user-connection and device-session tables.

Row classes only. Pydantic value objects, errors, and status literals live in
`user_connections.py` so they are importable from the module root without
being flagged as Row types by bin/sync_modules Rule-1.

Column types use `Text` (not `String`) to match the raw-SQL `TEXT` created by
the Alembic migration — alembic autogenerate detects `String` as `VARCHAR` vs
the DB's `TEXT` and flags a spurious drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UserOAuthConnectionRow(Base):
    """One row per (user_id, provider_id). Encrypted token columns hold Fernet
    ciphertext produced by `core/secrets.encrypt`. Plaintext tokens never persist."""

    __tablename__ = "user_oauth_connections"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    provider_id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_id_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    granted_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_refresh_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    needs_reauth_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # Partial index: only connected rows need the refresh sweep to check them.
        Index(
            "ix_user_oauth_connections_refresh",
            "last_refresh_at",
            postgresql_where=text("status = 'connected'"),
        ),
    )


class UserOAuthDeviceSessionRow(Base):
    """One pending device-auth handshake per (user_id, provider_id).
    Re-starting the flow replaces (upserts) the row. TTL is enforced via
    `expires_at`; expired rows are purged by `refresh_due_connections`."""

    __tablename__ = "user_oauth_device_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    provider_id: Mapped[str] = mapped_column(Text, primary_key=True)
    encrypted_device_code: Mapped[str] = mapped_column(Text, nullable=False)
    user_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
