"""SQLAlchemy models for `domain/orgs` — invitations, SSO config, coding agents.

`OrgRow` and `MembershipRow` have moved to `core/tenancy/models.py`.
`InvitationRow`, `SsoConfigRow`, and `OrgCodingAgentRow` remain here as
domain feature rows that reference the core tables by FK.
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
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InvitationRow(Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # SHA-256 of the signed invitation token. Raw tokens never live in the DB.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_invitations_email_lower", func.lower(email)),
        Index(
            "uq_invitations_pending_org_email",
            "org_id",
            func.lower(email),
            unique=True,
            postgresql_where=text("accepted_at IS NULL"),
        ),
    )


class SsoConfigRow(Base):
    __tablename__ = "sso_configs"

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    # Raw IdP metadata XML. SAML-only at POC.
    idp_metadata_xml: Mapped[str] = mapped_column(Text, nullable=False)
    jit_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Email-domain claims (lowercase, no `@`). When an org claims `acme.com`,
    # any user typing `*@acme.com` on the Login page gets routed to this
    # org's SSO. JSONB array; empty list = no claims.
    email_domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    exempt_owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Per-org SP private key, encrypted via `core/secrets`.
    sp_private_key_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sp_certificate: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OrgCodingAgentRow(Base):
    """Per-org installed coding-agent plugins. `settings` JSONB is plugin-shaped."""

    __tablename__ = "org_coding_agents"

    org_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    plugin_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
