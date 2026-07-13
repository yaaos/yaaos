"""SQLAlchemy row models for `domain/mcp_server` — four OAuth tables.

All token tables follow the bearer-discipline pattern: `token_hash TEXT PK`
(sha256 of the raw token); the raw token never persists.

Tables:
  mcp_oauth_clients   — dynamic client registrations (RFC 7591)
  mcp_auth_codes      — one-time authorization codes (10-minute TTL)
  mcp_access_tokens   — opaque access bearers (hours-scale TTL)
  mcp_refresh_tokens  — rotation tokens (weeks-scale TTL)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class McpOAuthClientRow(Base):
    __tablename__ = "mcp_oauth_clients"

    client_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    client_name: Mapped[str] = mapped_column(Text, nullable=False)
    # List of allowed redirect URIs for this client.
    redirect_uris: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class McpAuthCodeRow(Base):
    __tablename__ = "mcp_auth_codes"

    # sha256 hex of the raw one-time code. Raw never persists.
    code_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("mcp_oauth_clients.client_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # org_id fixed at consent time; the MCP principal carries this forever.
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # PKCE S256 code challenge; verified at token exchange.
    code_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class McpAccessTokenRow(Base):
    __tablename__ = "mcp_access_tokens"

    __table_args__ = (Index("idx_mcp_access_tokens_user", "user_id"),)

    # sha256 hex of the raw bearer. Raw returned to client once; never stored.
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("mcp_oauth_clients.client_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # org_id locked to consent-time selection; unchanged on refresh rotation.
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class McpRefreshTokenRow(Base):
    __tablename__ = "mcp_refresh_tokens"

    __table_args__ = (Index("idx_mcp_refresh_tokens_user", "user_id"),)

    # sha256 hex of the raw refresh token. Rotated on every use.
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("mcp_oauth_clients.client_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
