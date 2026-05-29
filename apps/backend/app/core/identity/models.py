"""SQLAlchemy models for `core/identity` — users, emails, oauth identities,
TOTP secrets, sessions, GitHub installations.

UUIDs are the universal primary key. Emails round-trip through `user_emails`;
the row in `users` is keyed by id, never email. Sessions store sha256-hashed
tokens, never raw secrets.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    # Verified GitHub login. Written by `plugins/github` (OAuth callback) on
    # every login, or via the verify-only flow in `domain/account`. Nullable
    # for SSO-only users.
    github_username: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft-delete only. Deactivated users keep their row so prior audit
    # references resolve and the user is re-invitable.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserEmailRow(Base):
    __tablename__ = "user_emails"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Verified_at None means a provider claimed the address but never confirmed.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Email uniqueness is global across non-deactivated users; deactivation
    # frees the address (lazy reuse). Postgres partial unique index lives in
    # the migration body since SQLAlchemy doesn't render partial indexes
    # cleanly across dialects.
    __table_args__ = (Index("ix_user_emails_email_lower", func.lower(email)),)


class OAuthIdentityRow(Base):
    __tablename__ = "oauth_identities"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    external_subject: Mapped[str] = mapped_column(String, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("provider", "external_subject", name="uq_oauth_identity_provider_subject"),
    )


class UserTotpSecretRow(Base):
    __tablename__ = "user_totp_secrets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # Fernet-encrypted base32 seed. Master key from env var; never logged.
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SessionRow(Base):
    """Opaque server-side sessions. PK is the sha256 hex of the raw token —
    raw tokens never live in the DB. One row per active session per principal."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Principal — exactly one of user_id / workspace_id is set (POC). Tokens
    # surface in ; the row shape extends with another nullable id then.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    # SSO satisfaction is per-session per-org. Sessions inherit no org by default.
    sso_satisfied_for_org_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    sso_satisfied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    # Double-submit CSRF — the cookie carries the session token; this is the
    # plaintext value the SPA echoes in X-CSRF-Token on mutations.
    csrf_token: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
